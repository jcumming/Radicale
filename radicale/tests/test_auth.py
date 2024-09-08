# This file is part of Radicale - CalDAV and CardDAV server
# Copyright © 2012-2016 Jean-Marc Martins
# Copyright © 2012-2017 Guillaume Ayoub
# Copyright © 2017-2022 Unrud <unrud@outlook.com>
# Copyright © 2024-2024 Peter Bieringer <pb@bieringer.de>
#
# This library is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Radicale.  If not, see <http://www.gnu.org/licenses/>.

"""
Radicale tests with simple requests and authentication.

"""

import os
import sys
from typing import Iterable, Tuple, Union

import pytest

from radicale import xmlutils
from radicale.tests import BaseTest


class TestBaseAuthRequests(BaseTest):
    """Tests basic requests with auth.

    We should setup auth for each type before creating the Application object.

    """

    def _test_htpasswd(self, htpasswd_encryption: str, htpasswd_content: str,
                       test_matrix: Union[str, Iterable[Tuple[str, str, bool]]]
                       = "ascii") -> None:
        """Test htpasswd authentication with user "tmp" and password "bepo" for
           ``test_matrix`` "ascii" or user "😀" and password "🔑" for
           ``test_matrix`` "unicode"."""
        htpasswd_file_path = os.path.join(self.colpath, ".htpasswd")
        encoding: str = self.configuration.get("encoding", "stock")
        with open(htpasswd_file_path, "w", encoding=encoding) as f:
            f.write(htpasswd_content)
        self.configure({"auth": {"type": "htpasswd",
                                 "htpasswd_filename": htpasswd_file_path,
                                 "htpasswd_encryption": htpasswd_encryption}})
        if test_matrix == "ascii":
            test_matrix = (("tmp", "bepo", True), ("tmp", "tmp", False),
                           ("tmp", "", False), ("unk", "unk", False),
                           ("unk", "", False), ("", "", False))
        elif test_matrix == "unicode":
            test_matrix = (("😀", "🔑", True), ("😀", "🌹", False),
                           ("😁", "🔑", False), ("😀", "", False),
                           ("", "🔑", False), ("", "", False))
        elif isinstance(test_matrix, str):
            raise ValueError("Unknown test matrix %r" % test_matrix)
        for user, password, valid in test_matrix:
            self.propfind("/", check=207 if valid else 401,
                          login="%s:%s" % (user, password))

    def test_htpasswd_plain(self) -> None:
        self._test_htpasswd("plain", "tmp:bepo")

    def test_htpasswd_plain_password_split(self) -> None:
        self._test_htpasswd("plain", "tmp:be:po", (
            ("tmp", "be:po", True), ("tmp", "bepo", False)))

    def test_htpasswd_plain_unicode(self) -> None:
        self._test_htpasswd("plain", "😀:🔑", "unicode")

    def test_htpasswd_md5(self) -> None:
        self._test_htpasswd("md5", "tmp:$apr1$BI7VKCZh$GKW4vq2hqDINMr8uv7lDY/")

    def test_htpasswd_md5_unicode(self):
        self._test_htpasswd(
            "md5", "😀:$apr1$w4ev89r1$29xO8EvJmS2HEAadQ5qy11", "unicode")

    def test_htpasswd_sha256(self) -> None:
        self._test_htpasswd("sha256", "tmp:$5$i4Ni4TQq6L5FKss5$ilpTjkmnxkwZeV35GB9cYSsDXTALBn6KtWRJAzNlCL/")

    def test_htpasswd_sha512(self) -> None:
        self._test_htpasswd("sha512", "tmp:$6$3Qhl8r6FLagYdHYa$UCH9yXCed4A.J9FQsFPYAOXImzZUMfvLa0lwcWOxWYLOF5sE/lF99auQ4jKvHY2vijxmefl7G6kMqZ8JPdhIJ/")

    def test_htpasswd_bcrypt(self) -> None:
        self._test_htpasswd("bcrypt", "tmp:$2y$05$oD7hbiQFQlvCM7zoalo/T.MssV3V"
                            "NTRI3w5KDnj8NTUKJNWfVpvRq")

    def test_htpasswd_bcrypt_unicode(self) -> None:
        self._test_htpasswd("bcrypt", "😀:$2y$10$Oyz5aHV4MD9eQJbk6GPemOs4T6edK"
                            "6U9Sqlzr.W1mMVCS8wJUftnW", "unicode")

    def test_htpasswd_multi(self) -> None:
        self._test_htpasswd("plain", "ign:ign\ntmp:bepo")

    @pytest.mark.skipif(sys.platform == "win32", reason="leading and trailing "
                        "whitespaces not allowed in file names")
    def test_htpasswd_whitespace_user(self) -> None:
        for user in (" tmp", "tmp ", " tmp "):
            self._test_htpasswd("plain", "%s:bepo" % user, (
                (user, "bepo", True), ("tmp", "bepo", False)))

    def test_htpasswd_whitespace_password(self) -> None:
        for password in (" bepo", "bepo ", " bepo "):
            self._test_htpasswd("plain", "tmp:%s" % password, (
                ("tmp", password, True), ("tmp", "bepo", False)))

    def test_htpasswd_comment(self) -> None:
        self._test_htpasswd("plain", "#comment\n #comment\n \ntmp:bepo\n\n")

    def test_htpasswd_lc_username(self) -> None:
        self.configure({"auth": {"lc_username": "True"}})
        self._test_htpasswd("plain", "tmp:bepo", (
            ("tmp", "bepo", True), ("TMP", "bepo", True), ("tmp1", "bepo", False)))

    def test_htpasswd_strip_domain(self) -> None:
        self.configure({"auth": {"strip_domain": "True"}})
        self._test_htpasswd("plain", "tmp:bepo", (
            ("tmp", "bepo", True), ("tmp@domain.example", "bepo", True), ("tmp1", "bepo", False)))

    def test_remote_user(self) -> None:
        self.configure({"auth": {"type": "remote_user"}})
        _, responses = self.propfind("/", """\
<?xml version="1.0" encoding="utf-8"?>
<propfind xmlns="DAV:">
    <prop>
        <current-user-principal />
    </prop>
</propfind>""", REMOTE_USER="test")
        assert responses is not None
        response = responses["/"]
        assert not isinstance(response, int)
        status, prop = response["D:current-user-principal"]
        assert status == 200
        href_element = prop.find(xmlutils.make_clark("D:href"))
        assert href_element is not None and href_element.text == "/test/"

    def test_http_x_remote_user(self) -> None:
        self.configure({"auth": {"type": "http_x_remote_user"}})
        _, responses = self.propfind("/", """\
<?xml version="1.0" encoding="utf-8"?>
<propfind xmlns="DAV:">
    <prop>
        <current-user-principal />
    </prop>
</propfind>""", HTTP_X_REMOTE_USER="test")
        assert responses is not None
        response = responses["/"]
        assert not isinstance(response, int)
        status, prop = response["D:current-user-principal"]
        assert status == 200
        href_element = prop.find(xmlutils.make_clark("D:href"))
        assert href_element is not None and href_element.text == "/test/"

    def http_x_auth_request_preferred_username(self) -> None:
        self.configure({"auth": {"type": "http_x_auth_request_preferred_username"}})
        _, responses = self.propfind("/", """\
<?xml version="1.0" encoding="utf-8"?>
<propfind xmlns="DAV:">
    <prop>
        <current-user-principal />
    </prop>
</propfind>""", HTTP_X_FORWARDED_PREFERRED_USERNAME="test")
        assert responses is not None
        response = responses["/"]
        assert not isinstance(response, int)
        status, prop = response["D:current-user-principal"]
        assert status == 200
        href_element = prop.find(xmlutils.make_clark("D:href"))
        assert href_element is not None and href_element.text == "/test/"

    def test_custom(self) -> None:
        """Custom authentication."""
        self.configure({"auth": {"type": "radicale.tests.custom.auth"}})
        self.propfind("/tmp/", login="tmp:")

    def test_none(self) -> None:
        self.configure({"auth": {"type": "none"}})
        self.propfind("/tmp/", login="tmp:")

    def test_denyall(self) -> None:
        self.configure({"auth": {"type": "denyall"}})
        self.propfind("/tmp/", login="tmp:", check=401)
