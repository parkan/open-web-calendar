#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2024 Nicco Kunzmann and Open Web Calendar Contributors <https://open-web-calendar.quelltext.eu/>
#
# SPDX-License-Identifier: GPL-2.0-only
from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import ParseResult, urlparse, quote
from werkzeug.datastructures import ImmutableMultiDict

import caldav
import requests
import yaml
import zoneinfo
from flask import (
    Flask,
    Response,
    jsonify,
    make_response,
    render_template,
    request,
    send_from_directory,
)
from flask_allowed_hosts import AllowedHosts
from flask_caching import Cache

from open_web_calendar.util import set_url_username_password

from . import translate, version
from .convert_to_dhtmlx import ConvertToDhtmlx
from .convert_to_ics import ConvertToICS
from .encryption import EmptyFernetStore, FernetStore

if TYPE_CHECKING:
    from open_web_calendar.conversion_base import ConversionStrategy


# configuration
def DEBUG() -> bool:  # noqa: N802
    """Wether we are in debug mode."""
    return os.environ.get("APP_DEBUG", "").lower() == "true"


PORT = int(os.environ.get("PORT", "5000"))
CACHE_REQUESTED_URLS_FOR_SECONDS = int(
    os.environ.get("CACHE_REQUESTED_URLS_FOR_SECONDS", 600)
)
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "").split(",")
if ALLOWED_HOSTS == [""]:  # noqa: SIM300, RUF100
    ALLOWED_HOSTS = []
REQUESTS_TIMEOUT = int(os.environ.get("SOURCE_TIMEOUT", "60"))

# constants
HERE = Path(__file__).parent
DEFAULT_SPECIFICATION_PATH = HERE / "default_specification.yml"
TEMPLATE_FOLDER_NAME = "templates"
TEMPLATE_FOLDER = HERE / TEMPLATE_FOLDER_NAME
CALENDARS_TEMPLATE_FOLDER_NAME = "calendars"
CALENDAR_TEMPLATE_FOLDER = TEMPLATE_FOLDER / CALENDARS_TEMPLATE_FOLDER_NAME
STATIC_FOLDER_NAME = "static"
STATIC_FOLDER_PATH = HERE / STATIC_FOLDER_NAME
DEFAULT_REQUEST_HEADERS = {
    "user-agent": "open-web-calendar",
}

# specification
PARAM_SPECIFICATION_URL = "specification_url"
TIMEZONES = list(zoneinfo.available_timezones())
TIMEZONES.sort()

# globals
app = Flask(__name__, template_folder="templates")

app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 31536000

# Check Configuring Flask-Cache section for more details
CACHE_CONFIG = {
    "CACHE_TYPE": "FileSystemCache",
    "CACHE_DIR": tempfile.mkdtemp(prefix="owc-cache-"),
}
cache = Cache(app, config=CACHE_CONFIG)

# caching

__URL_CACHE = {}


# limiting access
def host_not_allowed():
    return render_template(
        "403.html",
        hostname=request.host.split(":")[0],
        allowed_hosts=", ".join(ALLOWED_HOSTS),
    ), 403


allowed_hosts = AllowedHosts(
    app, allowed_hosts=ALLOWED_HOSTS, on_denied=host_not_allowed
)

# This is an in-app override of the default_specification.yml
DEFAULT_SPECIFICATION = {}


def cache_url(url, text):
    """Cache the value of a url."""
    __URL_CACHE[url] = text
    try:
        get_text_from_url(url)
    finally:
        del __URL_CACHE[url]


def encryption() -> FernetStore | EmptyFernetStore:
    return FernetStore.from_environment()


@app.after_request
def add_header(r):
    """
    Add headers to both force latest IE rendering engine or Chrome Frame,
    and also to cache the rendered page for 10 minutes.
    """
    if not request.path.startswith("/static/"):
        r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        r.headers["Pragma"] = "no-cache"
        r.headers["Expires"] = "0"
    else:
        r.headers["Cache-Control"] = "public, max-age=31536000"  # Cache for 1 year
        r.headers["Expires"] = (
            datetime.datetime.now() + datetime.timedelta(days=365)
        ).strftime("%a, %d %b %Y %H:%M:%S GMT")

    return r


# configuration


def get_configuration():
    """Return the configuration for the browser."""
    store = encryption()
    return {
        "default_specification": get_default_specification(),
        "version": version.version,
        "version-list": version.version_tuple,
        "timezones": TIMEZONES,
        "dhtmlx": {"languages": translate.dhtmlx_languages()},
        "index": {"languages": translate.languages_for_the_index_file()},
        "encryption": store.can_encrypt(),
    }


def set_js_headers(response):
    response = make_response(response)
    response.headers["Access-Control-Allow-Origin"] = "*"
    # see https://developer.mozilla.org/en-US/docs/Web/HTTP/CORS/Errors/CORSMissingAllowHeaderFromPreflight
    response.headers["Access-Control-Allow-Headers"] = request.headers.get(
        "Access-Control-Request-Headers"
    )
    if "Content-Type" not in response.headers:
        response.headers["Content-Type"] = "text/calendar"
    filename = request.args.get("filename")
    if filename:
        encoded_filename = quote(filename)
        response.headers.add("Content-Disposition", f"attachment; filename=\"{encoded_filename}\"")
    return response


def make_js_file_response(content: str) -> Response:
    """Modify the response to set the content type for .js files."""
    response = make_response(content)
    set_js_headers(response)
    response.headers["Content-Type"] = "text/javascript"
    return response


@cache.memoize(
    CACHE_REQUESTED_URLS_FOR_SECONDS, forced_update=lambda: bool(__URL_CACHE)
)
def get_text_from_url(url):
    """Return the text from a url.

    The result is cached CACHE_REQUESTED_URLS_FOR_SECONDS.
    """
    if __URL_CACHE:
        return __URL_CACHE[url]
    response = requests.get(
        url, headers=DEFAULT_REQUEST_HEADERS, timeout=REQUESTS_TIMEOUT
    )
    response.raise_for_status()
    return response.content


# @functools.cache
def get_specification_from_environment_variable(spec: Optional[str]) -> dict[str, Any]:
    """Return the specification from the env variable stored in a file."""
    if not spec:
        return {}
    path = Path(spec)
    if path.is_file():
        spec = path.read_text(encoding="UTF-8")
    ret = yaml.safe_load(spec + "\n")
    if not isinstance(ret, dict):
        raise ValueError(  # noqa: TRY004
            f"The specification {spec!r} is not a dictionary or a path to a file."
        )
    return ret


def get_default_specification() -> dict[str, Any]:
    """Return the default specification.

    The default specification is loaded from the default_specification.yml
    Then, values are overwritten from the environment variable OWC_SPECIFICATION.
    Then, values are overwritten from open_web_calendar.app.DEFAULT_SPECIFICATION.
    """
    with DEFAULT_SPECIFICATION_PATH.open(encoding="UTF-8") as file:
        spec = yaml.safe_load(file)
        env_spec = get_specification_from_environment_variable(
            os.environ.get("OWC_SPECIFICATION")
        )
        spec.update(env_spec)
        spec.update(DEFAULT_SPECIFICATION)
        return spec


def get_specification(query=None):
    """Build the calendar specification."""
    if query is None:
        query = request.args
    specification = get_default_specification()
    # get a request parameter, see https://stackoverflow.com/a/11774434
    url = query.get(PARAM_SPECIFICATION_URL, None)
    if url:
        url_specification_response = get_text_from_url(url)
        url_specification_values = yaml.safe_load(url_specification_response)
        specification.update(url_specification_values)
    for parameter in query:
        # get a list of arguments
        # see https://web.archive.org/web/20230325034825/https://werkzeug.palletsprojects.com/en/0.14.x/datastructures/
        value = query.getlist(parameter)
        # convert values
        for i in range(len(value)):
            if value[i] in ("false", "False"):
                value[i] = False
            elif value[i] in ("true", "True"):
                value[i] = True
        if len(value) == 1 and not isinstance(specification.get(parameter), list):
            value = value[0]
        specification[parameter] = value

    return specification


def get_query_string():
    return "?" + request.query_string.decode()


def render_app_template(template, specification):
    translation_file = Path(template).stem
    language = specification["language"]
    if specification["prefer_browser_language"]:
        # see https://stackoverflow.com/a/30441752/1320237
        # see https://tedboy.github.io/flask/generated/generated/werkzeug.LanguageAccept.html
        language = request.accept_languages.best_match(
            translate.LANGUAGE_CODES, language
        )
    return render_template(
        template,
        specification=specification,
        configuration=get_configuration(),
        json=json,
        get_query_string=get_query_string,
        html=lambda tid, **template_replacements: translate.html(
            language, translation_file, tid, **template_replacements
        ),
        string=lambda tid, **template_replacements: translate.string(
            language, translation_file, tid, **template_replacements
        ),
        language=language,
    )


def get_conversion(conversion: type[ConversionStrategy], specification: dict[str, Any]):
    """Return a conversion from the strategy."""
    strategy = conversion(specification, get_text_from_url, encryption(), DEBUG())
    strategy.retrieve_calendars()
    return set_js_headers(strategy.merge())


@app.route("/calendar.<ext>", methods=["GET", "OPTIONS"])
@allowed_hosts.limit()
# use query string in cache, see https://stackoverflow.com/a/47181782/1320237
# @cache.cached(timeout=CACHE_TIMEOUT, query_string=True)
def get_calendar(ext):
    """Return a calendar."""
    specification = get_specification()
    if ext == "spec":
        return jsonify(specification)
    if ext == "events.json":
        try:
            return get_conversion(ConvertToDhtmlx, specification)
        except:
            return json_error()
    if ext == "ics":
        return get_conversion(ConvertToICS, specification)
    if ext == "html":
        template_name = specification["template"]
        all_template_names = os.listdir(CALENDAR_TEMPLATE_FOLDER)
        assert template_name in all_template_names, (
            'Template names must be file names like "{}", not "{}".'.format(
                '", "'.join(all_template_names), template_name
            )
        )
        template = CALENDARS_TEMPLATE_FOLDER_NAME + "/" + template_name
        return render_app_template(template, specification)
    raise ValueError(
        f"Cannot use extension {ext}. Please see the documentation or report an error."
    )


for folder_path in STATIC_FOLDER_PATH.iterdir():
    if not folder_path.is_dir():
        continue

    @app.route(
        "/" + folder_path.name + "/<path:path>", endpoint="static/" + folder_path.name
    )
    def send_static(path, folder_name=folder_path.name):
        response = send_from_directory("static/" + folder_name, path)

        # Cache everything in static folder
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        response.headers["Expires"] = (
            datetime.datetime.now() + datetime.timedelta(days=365)
        ).strftime("%a, %d %b %Y %H:%M:%S GMT")
        response.headers["Pragma"] = "public"
        response.headers["Vary"] = "Accept-Encoding"

        return response


@app.route("/")
@app.route("/index.html")
@allowed_hosts.limit()
def serve_index():
    spec_url = "https://gitlab.com/lightandluck/open-web-calendar/-/snippets/4827957/raw/main/dweb-calendar-spec.json"
    query = ImmutableMultiDict([('specification_url', spec_url)])
    specification = get_specification(query)
    return render_app_template("index.html", specification)


@app.route("/about.html")
@allowed_hosts.limit()
def serve_about():
    specification = get_specification()
    return render_app_template("about.html", specification)


@app.route("/configuration.js")
@allowed_hosts.limit()
def serve_configuration():
    return make_js_file_response(
        f"/* generated */\nconst configuration = {json.dumps(get_configuration())};"
    )


@app.route("/locale_<lang>.js")
@allowed_hosts.limit()
def serve_locale(lang):
    """Serve the locale translations for the web frontend DHTMLX."""
    return make_js_file_response(
        render_template(
            "locale.js", locale=json.dumps(translate.dhtmlx(lang), indent="  ")
        )
    )


@app.errorhandler(500)
def unhandled_exception(error):
    """Called when an error occurs.

    See https://stackoverflow.com/q/14993318
    """
    trace = (
        f"<pre>\r\n{traceback.format_exc()}</pre>"
        if DEBUG()
        else "Trace only avalilable if DEBUG=true."
    )
    return (
        f"""
    <!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 3.2 Final//EN">
    <html>
        <head>
            <title>500 Internal Server Error</title>
        </head>
        <body>
            <h1>Internal Server Error</h1>
            <p>
                The server encountered an internal error and was unable to
                complete your request.  Either the server is overloaded or
                there is an error in the application.
            </p>
            {trace}
        </body>
    </html>
    """,
        http_status_code_for_error(error),
    )  # return error code from https://stackoverflow.com/a/7824605


def http_status_code_for_error(error: Exception) -> int:
    """Return the status code from an exception or 500."""
    return getattr(error, "http_status_code", 500)


def json_error():
    """Return the active exception as json."""
    _, err, _ = sys.exc_info()
    status_code = http_status_code_for_error(err)
    return jsonify(
        {
            "message": str(err) if DEBUG() else None,
            "traceback": traceback.format_exc() if DEBUG() else None,
            "error": type(err).__name__,
            "code": status_code,
        }
    ), status_code


@app.post("/encrypt")
def encrypt():
    """Return the JSON with the encrypted token."""
    try:
        store = FernetStore.from_environment()
        return jsonify({"token": store.encrypt(request.json)})
    except:
        return json_error()


@app.post("/decrypt")
def decrypt():
    """Return JSON with the decrypted token."""
    try:
        store = FernetStore.from_environment()
        token = request.json["token"]  # string
        passwords = request.json["passwords"]  # list of strings
        return jsonify(
            {
                "data": store.expose(token, passwords),
                "token": token,
            }
        )
    except:
        return json_error()


@app.get("/new-key")
def new_key():
    """Generate a new key."""
    store = FernetStore.from_environment()
    return store.generate_key()


@app.post("/caldav/list-calendars")
def list_caldav_calendars():
    """Return a list of caldav calendars."""
    try:
        url = request.json["url"]
        parsed: ParseResult = urlparse(url)
        username = request.json.get("username", parsed.username)
        password = request.json.get("password", parsed.password)
        with caldav.DAVClient(url=url, username=username, password=password) as client:
            # todo: sanitize Nextcloud by adding /remote.php/dav/
            principal = client.principal()
            calendars = principal.calendars()
            return jsonify(
                {
                    "calendars": [
                        {
                            "name": calendar.name,
                            "url": set_url_username_password(
                                calendar.url, username, password
                            ),
                        }
                        for calendar in calendars
                    ]
                }
            )
    except:
        return json_error()


# make serializable for multiprocessing
# app.__reduce__ = lambda: __name__ + ".app"


def main():
    """Run the Open Web Calendar"""
    print("""If you want to run the Open Web Calendar in production,
please use this command:

    gunicorn open_web_calendar:app
    """)  # noqa: T201
    app.run(debug=DEBUG(), host="0.0.0.0", port=PORT)


__all__ = [
    "DEFAULT_REQUEST_HEADERS",
    "DEFAULT_SPECIFICATION",
    "app",
    "cache_url",
    "get_default_specification",
    "get_specification",
    "get_text_from_url",
    "main",
]

if __name__ == "__main__":
    main()
