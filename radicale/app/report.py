# This file is part of Radicale - CalDAV and CardDAV server
# Copyright © 2008 Nicolas Kandel
# Copyright © 2008 Pascal Halter
# Copyright © 2008-2017 Guillaume Ayoub
# Copyright © 2017-2018 Unrud <unrud@outlook.com>
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

import contextlib
import copy
import datetime
import posixpath
import socket
import xml.etree.ElementTree as ET
import vobject
from http import client
from typing import (Any, Callable, Iterable, Iterator, List, Optional,
                    Sequence, Tuple, Union)
from urllib.parse import unquote, urlparse

import vobject.base
from vobject.base import ContentLine

import radicale.item as radicale_item
from radicale import httputils, pathutils, storage, types, xmlutils
from radicale.app.base import Access, ApplicationBase
from radicale.item import filter as radicale_filter
from radicale.log import logger

def free_busy_report(base_prefix: str, path: str, xml_request: Optional[ET.Element],
               collection: storage.BaseCollection, encoding: str,
               unlock_storage_fn: Callable[[], None]
               ) -> Tuple[int, str]:
    multistatus = ET.Element(xmlutils.make_clark("D:multistatus"))
    if xml_request is None:
        return client.MULTI_STATUS, multistatus
    root = xml_request
    if (root.tag == xmlutils.make_clark("C:free-busy-query") and
            collection.tag != "VCALENDAR"):
        logger.warning("Invalid REPORT method %r on %r requested",
                       xmlutils.make_human_tag(root.tag), path)
        return client.FORBIDDEN, xmlutils.webdav_error("D:supported-report")

    time_range_element = root.find(xmlutils.make_clark("C:time-range"))
    start,end = radicale_filter.time_range_timestamps(time_range_element)
    items = list(collection.get_by_time(start, end))

    cal = vobject.iCalendar()
    for item in items:
        occurrences = radicale_filter.time_range_fill(item.vobject_item, time_range_element, "VEVENT")
        for occurrence in occurrences:
            vfb = cal.add('vfreebusy')
            vfb.add('dtstamp').value = item.vobject_item.vevent.dtstamp.value
            vfb.add('dtstart').value, vfb.add('dtend').value = occurrence
    return (client.OK, cal.serialize())


def xml_report(base_prefix: str, path: str, xml_request: Optional[ET.Element],
               collection: storage.BaseCollection, encoding: str,
               unlock_storage_fn: Callable[[], None]
               ) -> Tuple[int, ET.Element]:
    """Read and answer REPORT requests that return XML.

    Read rfc3253-3.6 for info.

    """
    multistatus = ET.Element(xmlutils.make_clark("D:multistatus"))
    if xml_request is None:
        return client.MULTI_STATUS, multistatus
    root = xml_request
    if root.tag in (xmlutils.make_clark("D:principal-search-property-set"),
                    xmlutils.make_clark("D:principal-property-search"),
                    xmlutils.make_clark("D:expand-property")):
        # We don't support searching for principals or indirect retrieving of
        # properties, just return an empty result.
        # InfCloud asks for expand-property reports (even if we don't announce
        # support for them) and stops working if an error code is returned.
        logger.warning("Unsupported REPORT method %r on %r requested",
                       xmlutils.make_human_tag(root.tag), path)
        return client.MULTI_STATUS, multistatus
    if (root.tag == xmlutils.make_clark("C:calendar-multiget") and
            collection.tag != "VCALENDAR" or
            root.tag == xmlutils.make_clark("CR:addressbook-multiget") and
            collection.tag != "VADDRESSBOOK" or
            root.tag == xmlutils.make_clark("D:sync-collection") and
            collection.tag not in ("VADDRESSBOOK", "VCALENDAR")):
        logger.warning("Invalid REPORT method %r on %r requested",
                       xmlutils.make_human_tag(root.tag), path)
        return client.FORBIDDEN, xmlutils.webdav_error("D:supported-report")

    props: Union[ET.Element, List] = root.find(xmlutils.make_clark("D:prop")) or []

    hreferences: Iterable[str]
    if root.tag in (
            xmlutils.make_clark("C:calendar-multiget"),
            xmlutils.make_clark("CR:addressbook-multiget")):
        # Read rfc4791-7.9 for info
        hreferences = set()
        for href_element in root.findall(xmlutils.make_clark("D:href")):
            temp_url_path = urlparse(href_element.text).path
            assert isinstance(temp_url_path, str)
            href_path = pathutils.sanitize_path(unquote(temp_url_path))
            if (href_path + "/").startswith(base_prefix + "/"):
                hreferences.add(href_path[len(base_prefix):])
            else:
                logger.warning("Skipping invalid path %r in REPORT request on "
                               "%r", href_path, path)
    elif root.tag == xmlutils.make_clark("D:sync-collection"):
        old_sync_token_element = root.find(
            xmlutils.make_clark("D:sync-token"))
        old_sync_token = ""
        if old_sync_token_element is not None and old_sync_token_element.text:
            old_sync_token = old_sync_token_element.text.strip()
        logger.debug("Client provided sync token: %r", old_sync_token)
        try:
            sync_token, names = collection.sync(old_sync_token)
        except ValueError as e:
            # Invalid sync token
            logger.warning("Client provided invalid sync token %r: %s",
                           old_sync_token, e, exc_info=True)
            # client.CONFLICT doesn't work with some clients (e.g. InfCloud)
            return (client.FORBIDDEN,
                    xmlutils.webdav_error("D:valid-sync-token"))
        hreferences = (pathutils.unstrip_path(
            posixpath.join(collection.path, n)) for n in names)
        # Append current sync token to response
        sync_token_element = ET.Element(xmlutils.make_clark("D:sync-token"))
        sync_token_element.text = sync_token
        multistatus.append(sync_token_element)
    else:
        hreferences = (path,)
    filters = (
        root.findall(xmlutils.make_clark("C:filter")) +
        root.findall(xmlutils.make_clark("CR:filter")))

    # Retrieve everything required for finishing the request.
    retrieved_items = list(retrieve_items(
        base_prefix, path, collection, hreferences, filters, multistatus))
    collection_tag = collection.tag
    # !!! Don't access storage after this !!!
    unlock_storage_fn()

    while retrieved_items:
        # ``item.vobject_item`` might be accessed during filtering.
        # Don't keep reference to ``item``, because VObject requires a lot of
        # memory.
        item, filters_matched = retrieved_items.pop(0)
        if filters and not filters_matched:
            try:
                if not all(test_filter(collection_tag, item, filter_)
                           for filter_ in filters):
                    continue
            except ValueError as e:
                raise ValueError("Failed to filter item %r from %r: %s" %
                                 (item.href, collection.path, e)) from e
            except Exception as e:
                raise RuntimeError("Failed to filter item %r from %r: %s" %
                                   (item.href, collection.path, e)) from e

        found_props = []
        not_found_props = []

        for prop in props:
            element = ET.Element(prop.tag)
            if prop.tag == xmlutils.make_clark("D:getetag"):
                element.text = item.etag
                found_props.append(element)
            elif prop.tag == xmlutils.make_clark("D:getcontenttype"):
                element.text = xmlutils.get_content_type(item, encoding)
                found_props.append(element)
            elif prop.tag in (
                    xmlutils.make_clark("C:calendar-data"),
                    xmlutils.make_clark("CR:address-data")):
                element.text = item.serialize()

                expand = prop.find(xmlutils.make_clark("C:expand"))
                if expand is not None:
                    start = expand.get('start')
                    end = expand.get('end')

                    if (start is None) or (end is None):
                        return client.FORBIDDEN, \
                            xmlutils.webdav_error("C:expand")

                    start = datetime.datetime.strptime(
                        start, '%Y%m%dT%H%M%SZ'
                    ).replace(tzinfo=datetime.timezone.utc)
                    end = datetime.datetime.strptime(
                        end, '%Y%m%dT%H%M%SZ'
                    ).replace(tzinfo=datetime.timezone.utc)

                    expanded_element = _expand(
                        element, copy.copy(item), start, end)
                    found_props.append(expanded_element)
                else:
                    found_props.append(element)
            else:
                not_found_props.append(element)

        assert item.href
        uri = pathutils.unstrip_path(
            posixpath.join(collection.path, item.href))
        multistatus.append(xml_item_response(
            base_prefix, uri, found_props=found_props,
            not_found_props=not_found_props, found_item=True))

    return client.MULTI_STATUS, multistatus


def _expand(
        element: ET.Element,
        item: radicale_item.Item,
        start: datetime.datetime,
        end: datetime.datetime,
) -> ET.Element:
    dt_format = '%Y%m%dT%H%M%SZ'

    if type(item.vobject_item.vevent.dtstart.value) is datetime.date:
        # If an event comes to us with a dt_start specified as a date
        # then in the response we return the date, not datetime
        dt_format = '%Y%m%d'

    expanded_item, rruleset = _make_vobject_expanded_item(item, dt_format)

    if rruleset:
        recurrences = rruleset.between(start, end, inc=True)

        expanded: vobject.base.Component = copy.copy(expanded_item.vobject_item)
        is_expanded_filled: bool = False

        for recurrence_dt in recurrences:
            recurrence_utc = recurrence_dt.astimezone(datetime.timezone.utc)

            vevent = copy.deepcopy(expanded.vevent)
            vevent.recurrence_id = ContentLine(
                name='RECURRENCE-ID',
                value=recurrence_utc.strftime(dt_format), params={}
            )

            if is_expanded_filled is False:
                expanded.vevent = vevent
                is_expanded_filled = True
            else:
                expanded.add(vevent)

        element.text = expanded.serialize()
    else:
        element.text = expanded_item.vobject_item.serialize()

    return element


def _make_vobject_expanded_item(
        item: radicale_item.Item,
        dt_format: str,
) -> Tuple[radicale_item.Item, Optional[Any]]:
    # https://www.rfc-editor.org/rfc/rfc4791#section-9.6.5
    # The returned calendar components MUST NOT use recurrence
    #       properties (i.e., EXDATE, EXRULE, RDATE, and RRULE) and MUST NOT
    #       have reference to or include VTIMEZONE components.  Date and local
    #       time with reference to time zone information MUST be converted
    #       into date with UTC time.

    item = copy.copy(item)
    vevent = item.vobject_item.vevent

    if type(vevent.dtstart.value) is datetime.date:
        start_utc = datetime.datetime.fromordinal(
            vevent.dtstart.value.toordinal()
        ).replace(tzinfo=datetime.timezone.utc)
    else:
        start_utc = vevent.dtstart.value.astimezone(datetime.timezone.utc)

    vevent.dtstart = ContentLine(name='DTSTART', value=start_utc, params=[])

    dt_end = getattr(vevent, 'dtend', None)
    if dt_end is not None:
        if type(vevent.dtend.value) is datetime.date:
            end_utc = datetime.datetime.fromordinal(
                dt_end.value.toordinal()
            ).replace(tzinfo=datetime.timezone.utc)
        else:
            end_utc = dt_end.value.astimezone(datetime.timezone.utc)

        vevent.dtend = ContentLine(name='DTEND', value=end_utc, params={})

    rruleset = None
    if hasattr(item.vobject_item.vevent, 'rrule'):
        rruleset = vevent.getrruleset()

    # There is something strage behavour during serialization native datetime, so converting manualy
    vevent.dtstart.value = vevent.dtstart.value.strftime(dt_format)
    if dt_end is not None:
        vevent.dtend.value = vevent.dtend.value.strftime(dt_format)

    timezones_to_remove = []
    for component in item.vobject_item.components():
        if component.name == 'VTIMEZONE':
            timezones_to_remove.append(component)

    for timezone in timezones_to_remove:
        item.vobject_item.remove(timezone)

    try:
        delattr(item.vobject_item.vevent, 'rrule')
        delattr(item.vobject_item.vevent, 'exdate')
        delattr(item.vobject_item.vevent, 'exrule')
        delattr(item.vobject_item.vevent, 'rdate')
    except AttributeError:
        pass

    return item, rruleset


def xml_item_response(base_prefix: str, href: str,
                      found_props: Sequence[ET.Element] = (),
                      not_found_props: Sequence[ET.Element] = (),
                      found_item: bool = True) -> ET.Element:
    response = ET.Element(xmlutils.make_clark("D:response"))

    href_element = ET.Element(xmlutils.make_clark("D:href"))
    href_element.text = xmlutils.make_href(base_prefix, href)
    response.append(href_element)

    if found_item:
        for code, props in ((200, found_props), (404, not_found_props)):
            if props:
                propstat = ET.Element(xmlutils.make_clark("D:propstat"))
                status = ET.Element(xmlutils.make_clark("D:status"))
                status.text = xmlutils.make_response(code)
                prop_element = ET.Element(xmlutils.make_clark("D:prop"))
                for prop in props:
                    prop_element.append(prop)
                propstat.append(prop_element)
                propstat.append(status)
                response.append(propstat)
    else:
        status = ET.Element(xmlutils.make_clark("D:status"))
        status.text = xmlutils.make_response(404)
        response.append(status)

    return response


def retrieve_items(
        base_prefix: str, path: str, collection: storage.BaseCollection,
        hreferences: Iterable[str], filters: Sequence[ET.Element],
        multistatus: ET.Element) -> Iterator[Tuple[radicale_item.Item, bool]]:
    """Retrieves all items that are referenced in ``hreferences`` from
       ``collection`` and adds 404 responses for missing and invalid items
       to ``multistatus``."""
    collection_requested = False

    def get_names() -> Iterator[str]:
        """Extracts all names from references in ``hreferences`` and adds
           404 responses for invalid references to ``multistatus``.
           If the whole collections is referenced ``collection_requested``
           gets set to ``True``."""
        nonlocal collection_requested
        for hreference in hreferences:
            try:
                name = pathutils.name_from_path(hreference, collection)
            except ValueError as e:
                logger.warning("Skipping invalid path %r in REPORT request on "
                               "%r: %s", hreference, path, e)
                response = xml_item_response(base_prefix, hreference,
                                             found_item=False)
                multistatus.append(response)
                continue
            if name:
                # Reference is an item
                yield name
            else:
                # Reference is a collection
                collection_requested = True

    for name, item in collection.get_multi(get_names()):
        if not item:
            uri = pathutils.unstrip_path(posixpath.join(collection.path, name))
            response = xml_item_response(base_prefix, uri, found_item=False)
            multistatus.append(response)
        else:
            yield item, False
    if collection_requested:
        yield from collection.get_filtered(filters)


def test_filter(collection_tag: str, item: radicale_item.Item,
                filter_: ET.Element) -> bool:
    """Match an item against a filter."""
    if (collection_tag == "VCALENDAR" and
            filter_.tag != xmlutils.make_clark("C:%s" % filter_)):
        if len(filter_) == 0:
            return True
        if len(filter_) > 1:
            raise ValueError("Filter with %d children" % len(filter_))
        if filter_[0].tag != xmlutils.make_clark("C:comp-filter"):
            raise ValueError("Unexpected %r in filter" % filter_[0].tag)
        return radicale_filter.comp_match(item, filter_[0])
    if (collection_tag == "VADDRESSBOOK" and
            filter_.tag != xmlutils.make_clark("CR:%s" % filter_)):
        for child in filter_:
            if child.tag != xmlutils.make_clark("CR:prop-filter"):
                raise ValueError("Unexpected %r in filter" % child.tag)
        test = filter_.get("test", "anyof")
        if test == "anyof":
            return any(radicale_filter.prop_match(item.vobject_item, f, "CR")
                       for f in filter_)
        if test == "allof":
            return all(radicale_filter.prop_match(item.vobject_item, f, "CR")
                       for f in filter_)
        raise ValueError("Unsupported filter test: %r" % test)
    raise ValueError("Unsupported filter %r for %r" %
                     (filter_.tag, collection_tag))


class ApplicationPartReport(ApplicationBase):

    def do_REPORT(self, environ: types.WSGIEnviron, base_prefix: str,
                  path: str, user: str) -> types.WSGIResponse:
        """Manage REPORT request."""
        access = Access(self._rights, user, path)
        if not access.check("r"):
            return httputils.NOT_ALLOWED
        try:
            xml_content = self._read_xml_request_body(environ)
        except RuntimeError as e:
            logger.warning("Bad REPORT request on %r: %s", path, e,
                           exc_info=True)
            return httputils.BAD_REQUEST
        except socket.timeout:
            logger.debug("Client timed out", exc_info=True)
            return httputils.REQUEST_TIMEOUT
        with contextlib.ExitStack() as lock_stack:
            lock_stack.enter_context(self._storage.acquire_lock("r", user))
            item = next(iter(self._storage.discover(path)), None)
            if not item:
                return httputils.NOT_FOUND
            if not access.check("r", item):
                return httputils.NOT_ALLOWED
            if isinstance(item, storage.BaseCollection):
                collection = item
            else:
                assert item.collection is not None
                collection = item.collection

            if xml_content is not None and \
               xml_content.tag == xmlutils.make_clark("C:free-busy-query"):
                try:
                    status, body = free_busy_report(
                        base_prefix, path, xml_content, collection, self._encoding,
                        lock_stack.close)
                except ValueError as e:
                    logger.warning(
                        "Bad REPORT request on %r: %s", path, e, exc_info=True)
                    return httputils.BAD_REQUEST
                headers = {"Content-Type": "text/calendar; charset=%s" % self._encoding}
                return status, headers, body
            else:
                try:
                    status, xml_answer = xml_report(
                        base_prefix, path, xml_content, collection, self._encoding,
                        lock_stack.close)
                except ValueError as e:
                    logger.warning(
                        "Bad REPORT request on %r: %s", path, e, exc_info=True)
                    return httputils.BAD_REQUEST
                headers = {"Content-Type": "text/xml; charset=%s" % self._encoding}
                return status, headers, self._xml_response(xml_answer)
