import re
import json
import inspect

from markdown.extensions import Extension
from markdown.preprocessors import Preprocessor
from typing import Any, Dict, Optional, List
import markdown

import zerver.openapi.python_examples
from zerver.lib.openapi import get_openapi_fixture, openapi_spec, \
    get_openapi_parameters

MACRO_REGEXP = re.compile(r'\{generate_code_example(\(\s*(.+?)\s*\))*\|\s*(.+?)\s*\|\s*(.+?)\s*(\(\s*(.+)\s*\))?\}')
CODE_EXAMPLE_REGEX = re.compile(r'\# \{code_example\|\s*(.+?)\s*\}')

PYTHON_CLIENT_CONFIG = """
#!/usr/bin/env python3

import zulip

# Pass the path to your zuliprc file here.
client = zulip.Client(config_file="~/zuliprc")

"""

PYTHON_CLIENT_ADMIN_CONFIG = """
#!/usr/bin/env python

import zulip

# The user for this zuliprc file must be an organization administrator
client = zulip.Client(config_file="~/zuliprc-admin")

"""

DEFAULT_API_URL = "localhost:9991/api"
DEFAULT_AUTH_EMAIL = "BOT_EMAIL_ADDRESS"
DEFAULT_AUTH_API_KEY = "BOT_API_KEY"
DEFAULT_EXAMPLE = {
    "integer": 1,
    "string": "demo",
    "boolean": False,
}

def extract_python_code_example(source: List[str], snippet: List[str]) -> List[str]:
    start = -1
    end = -1
    for line in source:
        match = CODE_EXAMPLE_REGEX.search(line)
        if match:
            if match.group(1) == 'start':
                start = source.index(line)
            elif match.group(1) == 'end':
                end = source.index(line)
                break

    if (start == -1 and end == -1):
        return snippet

    snippet.extend(source[start + 1: end])
    snippet.append('    print(result)')
    snippet.append('\n')
    source = source[end + 1:]
    return extract_python_code_example(source, snippet)

def render_python_code_example(function: str, admin_config: Optional[bool]=False) -> List[str]:
    method = zerver.openapi.python_examples.TEST_FUNCTIONS[function]
    function_source_lines = inspect.getsourcelines(method)[0]

    if admin_config:
        config = PYTHON_CLIENT_ADMIN_CONFIG.splitlines()
    else:
        config = PYTHON_CLIENT_CONFIG.splitlines()

    snippet = extract_python_code_example(function_source_lines, [])

    code_example = []
    code_example.append('```python')
    code_example.extend(config)

    for line in snippet:
        # Remove one level of indentation and strip newlines
        code_example.append(line[4:].rstrip())

    code_example.append('```')

    return code_example

def curl_method_arguments(endpoint: str, method: str,
                          api_url: str) -> List[str]:
    method = method.upper()
    url = "{}/v1{}".format(api_url, endpoint)
    valid_methods = ["GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS"]
    if method == valid_methods[0]:
        # Then we need to make sure that each -d option translates to becoming
        # a GET parameter (in the URL) and not a POST parameter (in the body).
        # TODO: remove the -X part by updating the linting rule. It's redundant.
        return ["-X", "GET", "-G", url]
    elif method in valid_methods:
        return ["-X", method, url]
    else:
        msg = "The request method {} is not one of {}".format(method,
                                                              valid_methods)
        raise ValueError(msg)

def generate_curl_example(endpoint: str, method: str,
                          auth_email: str=DEFAULT_AUTH_EMAIL,
                          auth_api_key: str=DEFAULT_AUTH_API_KEY,
                          api_url: str=DEFAULT_API_URL) -> List[str]:
    lines = ["```curl"]
    openapi_entry = openapi_spec.spec()['paths'][endpoint][method.lower()]

    curl_first_line_parts = ["curl"] + curl_method_arguments(endpoint, method,
                                                             api_url)
    lines.append(" ".join(curl_first_line_parts))

    authentication_required = openapi_entry.get("security", False)
    if authentication_required:
        lines.append("    -u %s:%s" % (auth_email, auth_api_key))

    openapi_example_params = get_openapi_parameters(endpoint, method)
    for packet in openapi_example_params:
        param_name = packet["name"]
        param_type = packet["schema"]["type"]
        if param_type in ["object", "array"]:
            example_value = packet.get("example", None)
            if not example_value:
                msg = """All array and object type request parameters must have
concrete examples. The openAPI documentation for {}/{} is missing an example
value for the {} parameter. Without this we cannot automatically generate a
cURL example.""".format(endpoint, method, param_name)
                raise ValueError(msg)
            ordered_ex_val_str = json.dumps(example_value, sort_keys=True)
            line = "    --data-urlencode {}='{}'".format(param_name, ordered_ex_val_str)
        else:
            example_value = packet.get("example", DEFAULT_EXAMPLE[param_type])
            if type(example_value) == bool:
                example_value = str(example_value).lower()
            line = "    -d '{}={}'".format(param_name, example_value)
        lines.append(line)

    for i in range(1, len(lines)-1):
        lines[i] = lines[i] + " \\"

    lines.append("```")

    return lines

def render_curl_example(function: str) -> List[str]:
    """ A simple wrapper around generate_curl_example. """
    parts = function.split(":")
    endpoint = parts[0]
    method = parts[1]
    kwargs = dict()
    if len(parts) > 2:
        kwargs["auth_email"] = parts[2]
    if len(parts) > 3:
        kwargs["auth_api_key"] = parts[3]
    if len(parts) > 4:
        kwargs["api_url"] = parts[4]
    return generate_curl_example(endpoint, method, **kwargs)

SUPPORTED_LANGUAGES = {
    'python': {
        'client_config': PYTHON_CLIENT_CONFIG,
        'admin_config': PYTHON_CLIENT_ADMIN_CONFIG,
        'render': render_python_code_example,
    },
    'curl': {
        'render': render_curl_example
    }
}  # type: Dict[str, Any]

class APICodeExamplesGenerator(Extension):
    def extendMarkdown(self, md: markdown.Markdown, md_globals: Dict[str, Any]) -> None:
        md.preprocessors.add(
            'generate_code_example', APICodeExamplesPreprocessor(md, self.getConfigs()), '_begin'
        )

class APICodeExamplesPreprocessor(Preprocessor):
    def __init__(self, md: markdown.Markdown, config: Dict[str, Any]) -> None:
        super(APICodeExamplesPreprocessor, self).__init__(md)

    def run(self, lines: List[str]) -> List[str]:
        done = False
        while not done:
            for line in lines:
                loc = lines.index(line)
                match = MACRO_REGEXP.search(line)

                if match:
                    language = match.group(2)
                    function = match.group(3)
                    key = match.group(4)
                    argument = match.group(6)

                    if key == 'fixture':
                        if argument:
                            text = self.render_fixture(function, name=argument)
                        else:
                            text = self.render_fixture(function)
                    elif key == 'example':
                        if argument == 'admin_config=True':
                            text = SUPPORTED_LANGUAGES[language]['render'](function, admin_config=True)
                        else:
                            text = SUPPORTED_LANGUAGES[language]['render'](function)

                    # The line that contains the directive to include the macro
                    # may be preceded or followed by text or tags, in that case
                    # we need to make sure that any preceding or following text
                    # stays the same.
                    line_split = MACRO_REGEXP.split(line, maxsplit=0)
                    preceding = line_split[0]
                    following = line_split[-1]
                    text = [preceding] + text + [following]
                    lines = lines[:loc] + text + lines[loc+1:]
                    break
            else:
                done = True
        return lines

    def render_fixture(self, function: str, name: Optional[str]=None) -> List[str]:
        fixture = []

        # We assume that if the function we're rendering starts with a slash
        # it's a path in the endpoint and therefore it uses the new OpenAPI
        # format.
        if function.startswith('/'):
            path, method = function.rsplit(':', 1)
            fixture_dict = get_openapi_fixture(path, method, name)
        else:
            fixture_dict = zerver.openapi.python_examples.FIXTURES[function]

        fixture_json = json.dumps(fixture_dict, indent=4, sort_keys=True,
                                  separators=(',', ': '))

        fixture.append('```')
        fixture.extend(fixture_json.splitlines())
        fixture.append('```')

        return fixture

def makeExtension(*args: Any, **kwargs: str) -> APICodeExamplesGenerator:
    return APICodeExamplesGenerator(**kwargs)
