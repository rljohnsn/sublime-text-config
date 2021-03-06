from __future__ import print_function
import sublime
import sublime_plugin
import os
import sys
import json
import base64
import subprocess
import functools
import webbrowser
import tempfile
import traceback
import contextlib
import shutil
import re

try:
    import urllib2 as urllib
except ImportError:  # Python 3
    import urllib.request as urllib

DEFAULT_CREATE_PUBLIC_VALUE = 'false'
DEFAULT_USE_PROXY_VALUE = 'false'
settings = sublime.load_settings('Gist.sublime-settings')
GISTS_URL = 'https://api.github.com/gists'
USER_GISTS_URL = 'https://api.github.com/users/%s/gists'
STARRED = '/starred'
ORGS_URL = 'https://api.github.com/user/orgs'
ORG_MEMBERS_URL = 'https://api.github.com/orgs/%s/members'

#Enterprise support:
if settings.get('enterprise'):
    GISTS_URL = settings.get('url')
    if not GISTS_URL:
        raise MissingCredentialsException()
    GISTS_URL += '/api/v3/gists'

#Per page support (max 100)
if settings.get('max_gists'):
    if settings.get('max_gists') <= 100:
        MAX_GISTS = '?per_page=%d' % settings.get('max_gists')
        GISTS_URL += MAX_GISTS
        USER_GISTS_URL += MAX_GISTS
    else:
        settings.set("max_gists", 100)
        sublime.status_message("Gist: GitHub API does not support a value of higher than 100")


class MissingCredentialsException(Exception):
    pass


class CurlNotFoundException(Exception):
    pass


class SimpleHTTPError(Exception):
    def __init__(self, code, response):
        self.code = code
        self.response = response


class MissingTokenException(Exception):
    pass


def get_credentials():
    username = settings.get('username')
    password = settings.get('password')
    if not username or not password:
        raise MissingCredentialsException()
    return (username, password)


def basic_auth_string():
    auth_string = u'%s:%s' % get_credentials()
    return auth_string.encode('utf-8')


def get_token():
    token = settings.get('token')
    if not token:
        raise MissingTokenException()
    return token


def token_auth_string():
    auth_string = u'%s' % get_token()
    return auth_string.encode('utf-8')

if sublime.platform() == 'osx':
    # Keychain support
    # Instead of Gist.sublime-settings, fetch username and password from the user's github.com keychain entry
    SERVER = 'github.com'

    def create_keychain_accessor():
        from ctypes import cdll, util, c_uint32, c_int, c_char_p, c_void_p, POINTER, pointer, byref, Structure, string_at
        lib_security = cdll.LoadLibrary(util.find_library('Security'))

        class SecKeychainAttributeInfo(Structure):
            _fields_ = [("count", c_uint32), ("tag", POINTER(c_uint32)), ("format", POINTER(c_uint32))]

        class SecKeychainAttribute(Structure):
            _fields_ = [("tag", c_uint32), ("length", c_uint32), ("data", c_void_p)]

        class SecKeychainAttributeList(Structure):
            _fields_ = [("count", c_uint32), ("attr", POINTER(SecKeychainAttribute))]

        PtrSecKeychainAttributeList = POINTER(SecKeychainAttributeList)

        def keychain_get_credentials():
            username = settings.get('username')
            password = settings.get('password')
            if username and password:
                return (username, password)

            password_buflen = c_uint32()
            password_buf = c_void_p()
            item = c_void_p()

            error = lib_security.SecKeychainFindInternetPassword(
               None,  # keychain, NULL = default
               c_uint32(len(SERVER)),  # server name length
               c_char_p(SERVER),      # server name
               c_uint32(0),  # security domain - unused
               None,        # security domain - unused
               c_uint32(0 if not username else len(username)),  # account name length
               None if not username else c_char_p(username),   # account name
               c_uint32(0),  # path name length - unused
               None,         # path name
               c_uint32(0),  # port, 0 = any
               c_int(0),  # kSecProtocolTypeAny
               c_int(0),  # kSecAuthenticationTypeAny
               None,  # returned password length - unused
               None,  # returned password data - unused
               byref(item))  # returned keychain item reference
            if not error:
                info = SecKeychainAttributeInfo(
                    1,  # attribute count
                    pointer(c_uint32(1633903476)),  # kSecAccountItemAttr
                    pointer(c_uint32(6)))  # CSSM_DB_ATTRIBUTE_FORMAT_BLOB

                attrlist_ptr = PtrSecKeychainAttributeList()
                error = lib_security.SecKeychainItemCopyAttributesAndData(
                    item,  # keychain item reference
                    byref(info),  # list of attributes to retrieve
                    None,  # returned item class - unused
                    byref(attrlist_ptr),  # returned attribute data
                    byref(password_buflen),  # returned password length
                    byref(password_buf))  # returned password data

                if not error:
                    try:
                        if attrlist_ptr.contents.count == 1:
                            attr = attrlist_ptr.contents.attr.contents
                            username = string_at(attr.data, attr.length)
                            password = string_at(password_buf.value, password_buflen.value)
                    finally:
                        lib_security.SecKeychainItemFreeAttributesAndData(attrlist_ptr, password_buf)

            if not username or not password:
                raise MissingCredentialsException()
            else:
                return (username, password)

        return keychain_get_credentials
    get_credentials = create_keychain_accessor()


def catch_errors(fn):
    @functools.wraps(fn)
    def _fn(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except MissingCredentialsException:
            sublime.error_message("Gist: GitHub username or password isn't provided in Gist.sublime-settings file")
            user_settings_path = os.path.join(sublime.packages_path(), 'User', 'Gist.sublime-settings')
            if not os.path.exists(user_settings_path):
                default_settings_path = os.path.join(sublime.packages_path(), 'Gist', 'Gist.sublime-settings')
                shutil.copy(default_settings_path, user_settings_path)
            sublime.active_window().open_file(user_settings_path)
        except subprocess.CalledProcessError as err:
            sublime.error_message("Gist: Error while contacting GitHub: cURL returned %d" % err.returncode)
        except EnvironmentError as err:
            traceback.print_exc()
            if type(err) == OSError and err.errno == 2 and api_request == api_request_curl:
                sublime.error_message("Gist: Unable to find Python SSL module or cURL")
            else:
                msg = "Gist: Error while contacting GitHub"
                if err.strerror:
                    msg += err.strerror
                sublime.error_message(msg)
        except SimpleHTTPError as err:
            msg = "Gist: GitHub returned error %d" % err.code
            try:
                response_json = json.loads(err.response)
                response_msg = response_json.get('message')
                if response_msg:
                    msg += ": " + response_msg
            except ValueError:
                pass
            sublime.error_message(msg)
        except:
            traceback.print_exc()
            sublime.error_message("Gist: unknown error (please, report a bug!)")
    return _fn


def create_gist(public, description, files):
    file_data = dict((filename, {'content': text}) for filename, text in files.items())
    data = json.dumps({'description': description, 'public': public, 'files': file_data})
    gist = api_request(GISTS_URL, data)
    return gist


def update_gist(gist_url, file_changes, new_description=None):
    request = {'files': file_changes}
    # print('Request:', request)
    if new_description is not None:
        request['description'] = new_description
    data = json.dumps(request)
    # print('Data:', data)
    result = api_request(gist_url, data, method="PATCH")
    # print('Result:', result)
    return result


def gistify_view(view, gist, gist_filename):
    statusline_string = "Gist: " + gist_title(gist)[0]

    if not view.file_name():
        view.set_name(gist_filename)
    elif os.path.basename(view.file_name()) != gist_filename:
        statusline_string = "%s (%s)" % (statusline_string, gist_filename)

    view.settings().set('gist_html_url', gist["html_url"])
    view.settings().set('gist_description', gist['description'])
    view.settings().set('gist_url', gist["url"])
    view.settings().set('gist_filename', gist_filename)
    view.set_status("Gist", statusline_string)


def ungistify_view(view):
    view.settings().erase('gist_html_url')
    view.settings().erase('gist_description')
    view.settings().erase('gist_url')
    view.settings().erase('gist_filename')
    view.erase_status("Gist")


def open_gist(gist_url):
    gist = api_request(gist_url)
    # print('Gist:', gist)
    files = sorted(gist['files'].keys())

    for gist_filename in files:
        view = sublime.active_window().new_file()

        gistify_view(view, gist, gist_filename)

        edit = view.begin_edit()
        view.insert(edit, 0, gist['files'][gist_filename]['content'])
        view.end_edit(edit)

        if not "language" in gist['files'][gist_filename]:
            continue

        language = gist['files'][gist_filename]['language']

        if language is None:
            continue

        if language == 'C':
            new_syntax = os.path.join('C++', "{0}.tmLanguage".format(language))
        else:
            new_syntax = os.path.join(language, "{0}.tmLanguage".format(language))

        new_syntax_path = os.path.join(sublime.packages_path(), new_syntax)

        if os.path.exists(new_syntax_path):
            view.set_syntax_file(new_syntax_path)


def insert_gist(gist_url):
    gist = api_request(gist_url)
    files = sorted(gist['files'].keys())
    for gist_filename in files:
        view = sublime.active_window().active_view()
        edit = view.begin_edit()
        for region in view.sel():

            view.replace(edit, region, gist['files'][gist_filename]['content'])

        view.end_edit(edit)


def get_gists(url):
    return api_request(url)


def get_orgs():
    return api_request(ORGS_URL)


def get_org_members(org):
    return api_request(ORG_MEMBERS_URL % org)


def get_user_gists(user):
    return api_request(USER_GISTS_URL % user)


def gist_title(gist):
    file_name = sorted(gist['files'].keys())
    description = gist.get('description')
    title = description or file_name[0] or gist.get('id')

    if settings.get('show_authors'):
        return [title, gist.get('user').get('login')]
    else:
        return [title]


def gists_filter(all_gists):
    prefix = settings.get('gist_prefix')
    if prefix:
        prefix_len = len(prefix)

    if settings.get('gist_tag'):
        tag_prog = re.compile('(^|\s)#' + re.escape(settings.get('gist_tag')) + '($|\s)')
    else:
        tag_prog = False

    if not prefix and not tag_prog:
        return [all_gists, [gist_title(gist) for gist in all_gists]]

    gists = []
    gists_names = []
    for gist in all_gists:
        name = gist_title(gist)

        if prefix and name[0][0:prefix_len] == prefix:
            name[0] = name[0][prefix_len:]

            gists.append(gist)
            gists_names.append(name)

        elif tag_prog:
            match = re.search(tag_prog, name[0])
            if match:
                name[0] = name[0][0:match.start()] + name[0][match.end():]

                gists.append(gist)
                gists_names.append(name)

    return [gists, gists_names]


def api_request_native(url, data=None, method=None):
    request = urllib.Request(url)
    # print('API request url:', request.get_full_url())
    if method:
        request.get_method = lambda: method
    try:
        request.add_header('Authorization', 'token ' + token_auth_string())
    except MissingTokenException:
        request.add_header('Authorization', 'Basic ' + base64.urlsafe_b64encode(basic_auth_string()))
    request.add_header('Accept', 'application/json')
    request.add_header('Content-Type', 'application/json')

    if data is not None:
        request.add_data(data)

    # print('API request data:', request.get_data())
    # print('API request header:', request.header_items())
    if settings.get('https_proxy'):
        opener = urllib.build_opener(urllib.HTTPHandler(), urllib.HTTPSHandler(),
                                     urllib.ProxyHandler({'https': settings.get('https_proxy')}))

        urllib.install_opener(opener)

    try:
        with contextlib.closing(urllib.urlopen(request)) as response:
            if response.code == 204:  # No Content
                return None
            else:
                return json.loads(response.read())
    except urllib.HTTPError as err:
        with contextlib.closing(err):
            raise SimpleHTTPError(err.code, err.read())


@contextlib.contextmanager
def named_tempfile():
    tmpfile = tempfile.NamedTemporaryFile(delete=False)
    try:
        yield tmpfile
    finally:
        tmpfile.close()
        os.unlink(tmpfile.name)


def api_request_curl(url, data=None, method=None):
    command = ["curl", '-K', '-', url]

    try:
        config = ['--header "Authorization: token ' + token_auth_string() + '"',
              '--header "Accept: application/json"',
              '--header "Content-Type: application/json"',
              "--silent"]
    except MissingTokenException:
        config = ['-u ' + basic_auth_string(),
              '--header "Accept: application/json"',
              '--header "Content-Type: application/json"',
              "--silent"]

    if method:
        config.append('--request "%s"' % method)

    if settings.get('https_proxy'):
        config.append(settings.get('https_proxy'))

    with named_tempfile() as header_output_file:
        config.append('--dump-header "%s"' % header_output_file.name)
        header_output_file.close()
        with named_tempfile() as data_file:
            if data is not None:
                data_file.write(data)
                data_file.close()
                config.append('--data-binary "@%s"' % data_file.name)

            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            response, _ = process.communicate('\n'.join(config))
            returncode = process.returncode

            if returncode != 0:
                raise subprocess.CalledProcessError(returncode, 'curl')

            with open(header_output_file.name, "r") as headers:
                _, responsecode, message = headers.readline().split(None, 2)
                responsecode = int(responsecode)

                if responsecode == 204:  # No Content
                    return None
                elif 200 <= responsecode < 300 or responsecode == 100:  # Continue
                    return json.loads(response)
                else:
                    raise SimpleHTTPError(responsecode, response)

api_request = api_request_curl if ('ssl' not in sys.modules and os.name != 'nt') else api_request_native


class GistCommand(sublime_plugin.TextCommand):
    public = True

    def mode(self):
        return "Public" if self.public else "Private"

    @catch_errors
    def run(self, edit):
        try:
            get_token()
        except MissingTokenException:
            get_credentials()
        regions = [region for region in self.view.sel() if not region.empty()]

        if len(regions) == 0:
            regions = [sublime.Region(0, self.view.size())]
            gistify = True
        else:
            gistify = False

        region_data = [self.view.substr(region) for region in regions]

        window = self.view.window()

        def on_gist_description(description):
            filename = os.path.basename(self.view.file_name() if self.view.file_name() else '')

            @catch_errors
            def on_gist_filename(filename):
                # We need to figure out the filenames. Right now, the following logic is used:
                #   If there's only 1 selection, just pass whatever the user typed to Github. It'll rename empty files for us.
                #   If there are multiple selections and user entered a filename, rename the files from foo.js to
                #       foo (1).js, foo (2).js, etc.
                #   If there are multiple selections and user didn't enter anything, post the files as
                #       $SyntaxName 1, $SyntaxName 2, etc.
                if len(region_data) == 1:
                    gist_data = {filename: region_data[0]}
                else:
                    if filename:
                        (namepart, extpart) = os.path.splitext(filename)
                        make_filename = lambda num: "%s (%d)%s" % (namepart, num, extpart)
                    else:
                        syntax_name, _ = os.path.splitext(os.path.basename(self.view.settings().get('syntax')))
                        make_filename = lambda num: "%s %d" % (syntax_name, num)
                    gist_data = dict((make_filename(idx), data) for idx, data in enumerate(region_data, 1))

                gist = create_gist(self.public, description, gist_data)

                gist_html_url = gist['html_url']
                sublime.set_clipboard(gist_html_url)
                sublime.status_message("%s Gist: %s" % (self.mode(), gist_html_url))

                if gistify:
                    gistify_view(self.view, gist, gist['files'].keys()[0])
                # else:
                    # open_gist(gist['url'])

            window.show_input_panel('Gist File Name: (optional):', filename, on_gist_filename, None, None)

        window.show_input_panel("Gist Description (optional):", '', on_gist_description, None, None)


class GistViewCommand(object):
    """A base class for commands operating on a gistified view"""
    def is_enabled(self):
        return self.gist_url() is not None

    def gist_url(self):
        return self.view.settings().get("gist_url")

    def gist_html_url(self):
        return self.view.settings().get("gist_html_url")

    def gist_filename(self):
        return self.view.settings().get("gist_filename")

    def gist_description(self):
        return self.view.settings().get("gist_description")


class GistCopyUrl(GistViewCommand, sublime_plugin.TextCommand):
    def run(self, edit):
        sublime.set_clipboard(self.gist_html_url())


class GistOpenBrowser(GistViewCommand, sublime_plugin.TextCommand):
    def run(self, edit):
        webbrowser.open(self.gist_html_url())


class GistRenameFileCommand(GistViewCommand, sublime_plugin.TextCommand):
    def run(self, edit):
        old_filename = self.gist_filename()

        @catch_errors
        def on_filename(filename):
            if filename and filename != old_filename:
                text = self.view.substr(sublime.Region(0, self.view.size()))
                file_changes = {old_filename: {'filename': filename, 'content': text}}
                new_gist = update_gist(self.gist_url(), file_changes)
                gistify_view(self.view, new_gist, filename)
                sublime.status_message('Gist file renamed')

        self.view.window().show_input_panel('New File Name:', old_filename, on_filename, None, None)


class GistChangeDescriptionCommand(GistViewCommand, sublime_plugin.TextCommand):
    def run(self, edit):
        @catch_errors
        def on_gist_description(description):
            if description and description != self.gist_description():
                gist_url = self.gist_url()
                new_gist = update_gist(gist_url, {}, description)
                for window in sublime.windows():
                    for view in window.views():
                        if view.settings().get('gist_url') == gist_url:
                            gistify_view(view, new_gist, view.settings().get('gist_filename'))
                sublime.status_message('Gist description changed')

        self.view.window().show_input_panel('New Description:', self.gist_description() or '', on_gist_description, None, None)


class GistUpdateFileCommand(GistViewCommand, sublime_plugin.TextCommand):
    @catch_errors
    def run(self, edit):
        text = self.view.substr(sublime.Region(0, self.view.size()))
        changes = {self.gist_filename(): {'content': text}}
        update_gist(self.gist_url(), changes)
        sublime.status_message("Gist updated")


class GistDeleteFileCommand(GistViewCommand, sublime_plugin.TextCommand):
    @catch_errors
    def run(self, edit):
        changes = {self.gist_filename(): None}
        update_gist(self.gist_url(), changes)
        ungistify_view(self.view)
        sublime.status_message("Gist file deleted")


class GistDeleteCommand(GistViewCommand, sublime_plugin.TextCommand):
    @catch_errors
    def run(self, edit):
        gist_url = self.gist_url()
        api_request(gist_url, method='DELETE')
        for window in sublime.windows():
            for view in window.views():
                if view.settings().get("gist_url") == gist_url:
                    ungistify_view(view)
        sublime.status_message("Gist deleted")


class GistPrivateCommand(GistCommand):
    public = False


class GistListCommandBase(object):
    gists = orgs = users = []

    @catch_errors
    def run(self, *args):
        filtered = gists_filter(get_gists(GISTS_URL))
        parted = GISTS_URL.partition('?')
        STARRED_GISTS_URL = ''.join((parted[0] + STARRED, parted[1], parted[2]))
        filtered_stars = gists_filter(get_gists(STARRED_GISTS_URL))

        self.gists = filtered[0] + filtered_stars[0]
        gist_names = filtered[1] + map(lambda x: ["* " + x[0]], filtered_stars[1])

        if settings.get('include_users'):
            self.users = list(settings.get('include_users'))
            gist_names = ["> " + user for user in self.users] + gist_names

        if settings.get('include_orgs'):
            if settings.get('include_orgs') == True:
                self.orgs = [org.get("login") for org in get_orgs()]
            else:
                self.orgs = settings.get('include_orgs')

            gist_names = ["> " + org for org in self.orgs] + gist_names

        # print(gist_names)

        def on_gist_num(num):
            offOrgs = len(self.orgs)
            offUsers = offOrgs + len(self.users)

            if num < 0:
                pass
            elif num < offOrgs:
                self.gists = []

                members = [member.get("login") for member in get_org_members(self.orgs[num])]
                for member in members:
                    self.gists += get_user_gists(member)

                filtered = gists_filter(self.gists)
                self.gists = filtered[0]
                gist_names = filtered[1]
                # print(gist_names)

                self.orgs = self.users = []
                self.get_window().show_quick_panel(gist_names, on_gist_num)
            elif num < offUsers:
                filtered = gists_filter(get_user_gists(self.users[num - offOrgs]))
                self.gists = filtered[0]
                gist_names = filtered[1]
                # print(gist_names)

                self.orgs = self.users = []
                self.get_window().show_quick_panel(gist_names, on_gist_num)
            else:
                self.handle_gist(self.gists[num - offUsers])

        self.get_window().show_quick_panel(gist_names, on_gist_num)


class GistListCommand(GistListCommandBase, sublime_plugin.WindowCommand):
    @catch_errors
    def handle_gist(self, gist):
        open_gist(gist['url'])

    def get_window(self):
        return self.window


class InsertGistListCommand(GistListCommandBase, sublime_plugin.WindowCommand):
    @catch_errors
    def handle_gist(self, gist):
        insert_gist(gist['url'])

    def get_window(self):
        return self.window


class GistAddFileCommand(GistListCommandBase, sublime_plugin.TextCommand):
    def is_enabled(self):
        return self.view.settings().get('gist_url') is None

    def handle_gist(self, gist):
        @catch_errors
        def on_filename(filename):
            if filename:
                text = self.view.substr(sublime.Region(0, self.view.size()))
                changes = {filename: {'content': text}}
                new_gist = update_gist(gist['url'], changes)
                gistify_view(self.view, new_gist, filename)
                sublime.status_message("File added to Gist")

        filename = os.path.basename(self.view.file_name() if self.view.file_name() else '')
        self.view.window().show_input_panel('File Name:', filename, on_filename, None, None)

    def get_window(self):
        return self.view.window()
