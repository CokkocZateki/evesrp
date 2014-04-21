import datetime as dt
import time
from decimal import Decimal
from functools import partial
import re
import sys
from urllib.parse import urlparse, urlunparse, quote

import requests
from sqlalchemy import create_engine, Table, MetaData
from sqlalchemy.sql import select


class Killmail(object):
    """Base killmail representation.

    .. py:attribute:: kill_id

        The ID integer of this killmail. Used by most killboards and by CCP to
        refer to killmails.

    .. py:attribute:: ship_id

        The typeID integer of for the ship lost for this killmail.

    .. py:attribute:: ship

        The human readable name of the ship lost for this killmail.

    .. py:attribute:: pilot_id

        The ID number of the pilot who lost the ship. Referred to by CCP as
        ``characterID``.

    .. py:attribute:: pilot

        The name of the pilot who lost the ship.

    .. py:attribute:: corp_id

        The ID number of the corporation :py:attr:`pilot` belonged to at the
        time this kill happened.

    .. py:attribute:: corp

        The name of the corporation referred to by :py:attr:`corp_id`.

    .. py:attribute:: alliance_id

        The ID number of the alliance :py:attr:`corp` belonged to at the time
        of this kill, or ``None`` if the corporation wasn't in an alliance at
        the time.

    .. py:attribute:: alliance

        The name of the alliance referred to by :py:attr:`alliance_id`.

    .. py:attribute:: url

        A URL for viewing this killmail's information later. Typically an
        online killboard such as `zKillboard <https://zkillboard.com>`, but
        other kinds of links may be used.

    .. py:attribute:: value

        The extimated ISK loss for the ship destroyed in this killmail. This is
        an optional attribute, and is ``None`` if unsupported. If this
        attribute is set, it should be a floating point number (or something
        like it, like :py:class:`decimal.Decimal`) representing millions of
        ISK.

    .. py:attribute:: timestamp

        The date and time that this kill occured as a
        :py:class:`datetime.datetime` object (with a UTC timezone).

    .. py:attribute:: verified

        Whether or not this killmail has been API verified (or more accurately,
        if it is to be trusted when making a
        :py:class:`~evesrp.models.Request`.
    """

    def __init__(self, **kwargs):
        """Initialize a :py:class:`Killmail` with ``None`` for all attributes.

        All subclasses of this class, (and all mixins designed to be used with
        it) must call ``super().__init__(**kwargs)`` to ensure all
        initialization is done.

        :param: keyword arguments corresponding to attributes.
        """
        super(Killmail, self).__init__(**kwargs)
        for attr in ('kill_id', 'ship_id', 'ship', 'pilot_id', 'pilot',
                'corp_id', 'corp', 'alliance_id', 'alliance', 'verified',
                'url', 'value', 'timestamp'):
            try:
                setattr(self, attr, kwargs[attr])
            except KeyError:
                try:
                    setattr(self, attr, None)
                except AttributeError:
                    pass

    def __str__(self):
        return "{kill_id}: {pilot} lost a {ship}. Verified: {verified}.".\
                format(kill_id=self.kill_id, pilot=self.pilot, ship=self.ship,
                        verified=self.verified)

    def __iter__(self):
        """Iterate over the attributes of this killmail.

        Yields tuples in the form ``('<name>', <value>)``. This is used by
        :py:meth:`evesrp.models.Request.__init__` to initialize its data
        quickly. The `<name>` in the returned tuples is the name of the
        attribute on the :py:class:`~evesrp.models.Request`.
        """
        yield ('id', self.kill_id)
        yield ('ship_type', self.ship)
        yield ('corporation', self.corp)
        yield ('alliance', self.alliance)
        yield ('killmail_url', self.url)
        yield ('base_payout', self.value)
        yield ('kill_timestamp', self.timestamp)


class RequestsSessionMixin(object):
    """Mixin for providing a :py:class:`requests.Session`.

    The shared session allows HTTP user agents to be set properly, and for
    possible connection pooling.

    .. py:attribute:: requests_session

        A :py:class:`~requests.Session` for making HTTP requests.
    """
    def __init__(self, requests_session=None, **kwargs):
        """Set up a :py:class:`~requests.Session` for making HTTP requests.

        If an existing session is not provided, one will be created.

        :param requests_session: an existing session to use.
        :type requests: :py:class:`~requests.Session`
        """
        if requests_session is None:
            self.requests_session = requests.Session()
        else:
            self.requests_session = request_session
        super(RequestsSessionMixin, self).__init__(**kwargs)


def SQLShipMixin(*args, **kwargs):
    """Class factory for mixin classes to retrieve ship names from a database.

    Uses SQLAlchemy internally for SQL operations so as to make it usable on as
    many platforms as possible. The arguments passed to this function are
    passed directly to :py:func:`sqlalchemy.create_engine`, so feel free to use
    whatver arguemtns you wish. As long as the database has an ``invTypes``
    table with ``typeID`` and ``typeName`` columns and there's a DBAPI driver
    supported by SQLAlchemy, this mixin should work.
    """
    class _SQLMixin(object):
        #: The :py:class:`sqlalchemy.engine.Engine` instance for the database.
        engine = create_engine(*args, **kwargs)

        #: The :py:class:`sqlalchemy.MetaData <sqlalchemy.schema.MetaData>`
        #: instance for the database.
        metadata = MetaData(bind=engine)

        #: The :py:class:`sqlalchemy.Table <sqlalchemy.schema.Table>`
        #: representing the invTypes table. Column definitions are reflected at
        #: run time.
        invTypes = Table('invTypes', metadata, autoload=True)

        def __init__(self, *args, **kwargs):
            super(_SQLMixin, self).__init__(*args, **kwargs)

        @property
        def ship(self):
            """Looks up the ship name for :py:attr:`~Killmail.ship_id` in an
            SQL database.
            """
            conn = self.engine.connect()
            # Construct the select statement
            sel = select([self.invTypes.c.typeName])
            sel = sel.where(self.invTypes.c.typeID == self.ship_id)
            sel = sel.limit(1)
            # Get the results
            result = conn.execute(sel)
            row = result.fetchone()
            # Cleanup
            result.close()
            conn.close()
            return row[0]

    return _SQLMixin


class EveMDShipNameMixin(RequestsSessionMixin):
    """Killmail mixin for getting ship names using ship IDs using
    eve-marketdata.com.

    This method can be a slow method for looking up ship names, but it's fairly
    reliable.
    """
    # Yeah, using regexes on XML. Deal with it.
    evemd_regex = re.compile(
            r'<val id="\d+">(?P<ship_name>[A-Za-z0-9]+)</val>')

    def __init__(self, user_agent=None, **kwargs):
        """Setup a :py:class:`Killmail` that looks up ship names from
        eve-market-data.

        :param user_agent str: Character name (or some other way to contact
            you).
        """
        if user_agent is not None or user_agent != '':
            self.user_agent=user_agent
        else:
            self.user_agent = 'Unconfigured EVE-SRP Mixin'
        super(EveMDShipNameMixin, self).__init__(**kwargs)

    @property
    def ship(self):
        """Looks up the ship name for :py:attr:`~Killmail.ship_id` using
        eve-marketdata.com's API.
        """
        resp = self.requests_session.get(
                'http://api.eve-marketdata.com/api/type_name.xml', params=
                {
                    'char_name': quote(self.user_agent),
                    'v': self.ship_id
                })
        if resp.status_code == requests.codes.ok:
            match = self.evemd_regex.search(resp.text)
            if match:
                return match.group('ship_name')
        return None


class ZKillmail(Killmail, RequestsSessionMixin):
    """A killmail sourced from a zKillboard based killboard."""

    zkb_regex = re.compile(r'/detail/(?P<kill_id>\d+)/?')

    def __init__(self, url, **kwargs):
        """Create a killmail from the given URL.

        :param str url: The URL of the killmail.
        :raises ValueError: if ``url`` isn't a valid zKillboard killmail.
        :raises LookupError: if the zKillboard API response is in an unexpected
            format.
        """
        super(ZKillmail, self).__init__(**kwargs)
        self.url = url
        match = self.zkb_regex.search(url)
        if match:
            self.kill_id = int(match.group('kill_id'))
        else:
            raise ValueError("Killmail ID was not found in URL '{}'".
                    format(self.url))
        parsed = urlparse(self.url, scheme='https')
        if parsed.netloc == '':
            # Just in case someone is silly and gives an address without a
            # scheme. Also fix self.url to have a scheme.
            parsed = urlparse('//' + url, scheme='https')
            self.url = urlunparse(parsed)
        self.domain = parsed.netloc
        # Check API
        api_url = [a for a in parsed]
        api_url[2] = '/api/killID/{}'.format(self.kill_id)
        resp = self.requests_session.get(urlunparse(api_url))
        try:
            json = resp.json()[0]
        except ValueError as e:
            raise LookupError("Error retrieving killmail data: {}"
                    .format(resp.status_code)) from e
        victim = json['victim']
        self.pilot_id = victim['characterID']
        self.pilot = victim['characterName']
        self.corp_id = victim['corporationID']
        self.corp = victim['corporationName']
        self.alliance_id = victim['allianceID']
        self.alliance = victim['allianceName']
        self.ship_id = victim['shipTypeID']
        # For consistency, store self.value in millions. Decimal is being used
        # for precision at large values.
        value = Decimal(json['zkb']['totalValue'])
        self.value = value / 1000000
        # Parse the timestamp
        time_struct = time.strptime(json['killTime'], '%Y-%m-%d %H:%M:%S')
        self.timestamp = dt.datetime(*(time_struct[0:6]),
                tzinfo=dt.timezone.utc)

    @property
    def verified(self):
        return self.kill_id > 0

    def __str__(self):
        parent = super(ZKillmail, self).__str__()
        return "{parent} From ZKillboard at {url}".format(parent=parent,
                url=self.url)


class CRESTMail(Killmail, RequestsSessionMixin):
    """A killmail with data sourced from a CREST killmail link."""

    crest_regex = re.compile(r'/killmails/(?P<kill_id>\d+)/[0-9a-f]+/')

    def __init__(self, url, **kwargs):
        """Create a killmail from a CREST killmail link.

        :param str url: the CREST killmail URL.
        :raises ValueError: if ``url`` is not a CREST URL.
        :raises LookupError: if the CREST API response is in an unexpected
            format.
        """
        super(CRESTMail, self).__init__(**kwargs)
        self.url = url
        match = self.crest_regex.search(self.url)
        if match:
            self.kill_id = match.group('kill_id')
        else:
            raise ValueError("Killmail ID was not found in URL '{}'".
                    format(self.url))
        parsed = urlparse(self.url, scheme='https')
        if parsed.netloc == '':
            parsed = urlparse('//' + url, scheme='https')
            self.url = urlunparse(parsed)
        # Check if it's a valid CREST URL
        resp = self.requests_session.get(self.url)
        try:
            json = resp.json()
        except ValueError as e:
            raise LookupError("Error retrieving killmail data: {}"
                    .format(resp.status_code)) from e
        victim = json['victim']
        char = victim['character']
        corp = victim['corporation']
        ship = victim['shipType']
        alliance = victim['alliance']
        self.pilot_id = char['id']
        self.pilot = char['name']
        self.corp_id = corp['id']
        self.corp = corp['name']
        self.alliance_id = alliance['id']
        self.alliance = alliance['name']
        self.ship_id = ship['id']
        self.ship = ship['name']
        # CREST Killmails are always verified
        self.verified = True
        # Parse the timestamp
        time_struct = time.strptime(json['killTime'], '%Y.%m.%d %H:%M:%S')
        self.timestamp = dt.datetime(*(time_struct[0:6]),
                tzinfo=dt.timezone.utc)