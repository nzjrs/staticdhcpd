# -*- encoding: utf-8 -*-
"""
staticDHCPd module: src.sql

Purpose
=======
 Provides a uniform datasource API, selecting from multiple backends,
 for a staticDHCPd server.
 
Legal
=====
 This file is part of staticDHCPd.
 staticDHCPd is free software; you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation; either version 3 of the License, or
 (at your option) any later version.

 This program is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 GNU General Public License for more details.

 You should have received a copy of the GNU General Public License
 along with this program. If not, see <http://www.gnu.org/licenses/>.
 
 (C) Neil Tallim, 2011 <red.hamsterx@gmail.com>
 (C) Matthew Boedicker, 2011 <matthewm@boedicker.org>
"""
################################################################################
#   The decision of which engine to use occurs at the bottom of this module    #
# The chosen class is made accessible via the module-level SQL_BROKER variable #
#   The chosen module is accessible via the module-level SQL_MODULE variable   #
################################################################################
import threading
import ConfigParser
import os
import signal

import src.conf_buffer as conf
import src.logging

class _SQLBroker(object):
    """
    A stub documenting the features an _SQLBroker object must provide.
    """
    _resource_lock = None #: A lock used to prevent the database from being overwhelmed.
    _cache_lock = None #: A lock used to prevent multiple simultaneous cache updates.
    _mac_cache = None #: A cache used to prevent unnecessary database hits.
    _subnet_cache = None #: A cache used to prevent unnecessary database hits.
    
    def _getConnection(self):
        """
        Provides a connection to the database.

        @return: The connection object to be used.

        @raise Exception: If a problem occurs while accessing the database.
        """
        raise NotImplementedError("_getConnection must be overridden")
        
    def _setupBroker(self, concurrency_limit):
        """
        Sets up common attributes of broker objects.
        
        @type concurrency_limit: int
        @param concurrent_limit: The number of concurrent database hits to
            permit.
        """
        self._resource_lock = threading.BoundedSemaphore(concurrency_limit)
        self._setupCache()
        
    def _setupCache(self):
        """
        Sets up the SQL broker cache.
        """
        self._cache_lock = threading.Lock()
        self._mac_cache = {}
        self._subnet_cache = {}
        
    def flushCache(self):
        """
        Resets the cache to an empty state, forcing all lookups to pull fresh
        data.
        """
        if conf.USE_CACHE:
            self._cache_lock.acquire()
            try:
                self._mac_cache = {}
                self._subnet_cache = {}
                src.logging.writeLog("Flushed DHCP cache")
            finally:
                self._cache_lock.release()
                
    def lookupMAC(self, mac):
        """
        Queries the database for the given MAC address and returns the IP and
        associated details if the MAC is known.
        
        If enabled, the cache is checked and updated by this function.
        
        @type mac: basestring
        @param mac: The MAC address to lookup.
        
        @rtype: tuple(11)|None
        @return: (ip:basestring, hostname:basestring|None,
            gateway:basestring|None, subnet_mask:basestring|None,
            broadcast_address:basestring|None,
            domain_name:basestring|None, domain_name_servers:basestring|None,
            ntp_servers:basestring|None, lease_time:int,
            subnet:basestring, serial:int) or None if no match was
            found.
        
        @raise Exception: If a problem occurs while accessing the database.
        """
        if conf.USE_CACHE:
            self._cache_lock.acquire()
            try:
                data = self._mac_cache.get(mac)
                if data:
                    (ip, hostname, subnet_id) = data
                    return (ip, hostname,) + self._subnet_cache[subnet_id] + subnet_id
            finally:
                self._cache_lock.release()
                
        self._resource_lock.acquire()
        try:
            data = self._lookupMAC(mac)
            if conf.USE_CACHE:
                if data:
                    (ip, hostname,
                     gateway, subnet_mask, broadcast_address,
                     domain_name, domain_name_servers, ntp_servers,
                     lease_time, subnet, serial) = data
                    subnet_id = (subnet, serial)
                    self._cache_lock.acquire()
                    try:
                        self._mac_cache[mac] = (ip, hostname, subnet_id,)
                        self._subnet_cache[subnet_id] = (
                         gateway, subnet_mask, broadcast_address,
                         domain_name, domain_name_servers, ntp_servers,
                         lease_time,
                        )
                    finally:
                        self._cache_lock.release()
            return data
        finally:
            self._resource_lock.release()
            
class _DB20Broker(_SQLBroker):
    """
    Defines bevahiour for a DB API 2.0-compatible broker.
    """
    _module = None #: The db2api-compliant module to use.
    _connection_details = None #: The module-specific details needed to connect to a database.
    _query_mac = None #: The string used to look up a MAC's binding.
    
    def _lookupMAC(self, mac):
        """
        Queries the database for the given MAC address and returns the IP and
        associated details if the MAC is known.
        
        @type mac: basestring
        @param mac: The MAC address to lookup.
        
        @rtype: tuple(11)|None
        @return: (ip:basestring, hostname:basestring|None,
            gateway:basestring|None, subnet_mask:basestring|None,
            broadcast_address:basestring|None,
            domain_name:basestring|None, domain_name_servers:basestring|None,
            ntp_servers:basestring|None, lease_time:int,
            subnet:basestring, serial:int) or None if no match was
            found.
        
        @raise Exception: If a problem occurs while accessing the database.
        """
        try:
            db = self._getConnection()
            cur = db.cursor()
            
            cur.execute(self._query_mac, (mac,))
            result = cur.fetchone()
            if result:
                return result
            return None
        finally:
            try:
                cur.close()
            except Exception:
                pass
            try:
                db.close()
            except Exception:
                pass
                
class _PoolingBroker(_DB20Broker):
    """
    Defines bevahiour for a connection-pooling-capable DB API 2.0-compatible
    broker.
    """
    _pool = None #: The database connection pool.
    _eventlet__db_pool = None #: A reference to the eventlet.db_pool module.
    
    def _setupBroker(self, concurrency_limit):
        """
        Sets up connection-pooling, if it's supported by the environment.
        
        Also completes the broker-setup process.
        
        L{_connection_details} must be defined before calling this function.
        
        @type concurrency_limit: int
        @param concurrent_limit: The number of concurrent database hits to
            permit.
        """
        _DB20Broker._setupBroker(self, concurrency_limit)

        if conf.USE_POOL:
            try:
                import eventlet.db_pool
                self._eventlet__db_pool = eventlet.db_pool
            except ImportError:
                return
            else:
                self._pool = self._eventlet__db_pool.ConnectionPool(
                 SQL_MODULE,
                 max_size=concurrency_limit, max_idle=30, max_age=600, connect_timeout=5,
                 **self._connection_details
                )
                
    def _getConnection(self):
        """
        Provides a connection to the database.

        @return: The connection object to be used.
        
        @raise Exception: If a problem occurs while accessing the database.
        """
        if not self._pool is None:
            return self._eventlet__db_pool.PooledConnectionWrapper(self._pool.get(), self._pool)
        else:
            return SQL_MODULE.connect(**self._connection_details)
            
class _NonPoolingBroker(_DB20Broker):
    """
    Defines bevahiour for a non-connection-pooling-capable DB API 2.0-compatible
    broker.
    """
    
    def _getConnection(self):
        """
        Provides a connection to the database.

        @return: The connection object to be used.
        
        @raise Exception: If a problem occurs while accessing the database.
        """
        return SQL_MODULE.connect(**self._connection_details)
        
class _MySQL(_PoolingBroker):
    """
    Implements a MySQL broker.
    """
    _query_mac = """
     SELECT
      m.ip, m.hostname,
      s.gateway, s.subnet_mask, s.broadcast_address, s.domain_name, s.domain_name_servers,
      s.ntp_servers, s.lease_time, s.subnet, s.serial
     FROM maps m, subnets s
     WHERE
      m.mac = %s AND m.subnet = s.subnet AND m.serial = s.serial
     LIMIT 1
    """
    
    def __init__(self):
        """
        Constructs the broker.
        """
        self._connection_details = {
         'db': conf.MYSQL_DATABASE,
         'user': conf.MYSQL_USERNAME,
         'passwd': conf.MYSQL_PASSWORD,
        }
        if conf.MYSQL_HOST is None:
            self._connection_details['host'] = 'localhost'
        else:
            self._connection_details['host'] = conf.MYSQL_HOST
            self._connection_details['port'] = conf.MYSQL_PORT
            
        self._setupBroker(conf.MYSQL_MAXIMUM_CONNECTIONS)
        
class _PostgreSQL(_PoolingBroker):
    """
    Implements a PostgreSQL broker.
    """
    _query_mac = """
     SELECT
      m.ip, m.hostname,
      s.gateway, s.subnet_mask, s.broadcast_address, s.domain_name, s.domain_name_servers,
      s.ntp_servers, s.lease_time, s.subnet, s.serial
     FROM maps m, subnets s
     WHERE
      m.mac = %s AND m.subnet = s.subnet AND m.serial = s.serial
     LIMIT 1
    """
    
    def __init__(self):
        """
        Constructs the broker.
        """
        self._connection_details = {
         'database': conf.POSTGRESQL_DATABASE,
         'user': conf.POSTGRESQL_USERNAME,
         'password': conf.POSTGRESQL_PASSWORD,
        }
        if not conf.POSTGRESQL_HOST is None:
            self._connection_details['host'] = conf.POSTGRESQL_HOST
            self._connection_details['port'] = conf.POSTGRESQL_PORT
            self._connection_details['sslmode'] = conf.POSTGRESQL_SSLMODE
            
        self._setupBroker(conf.POSTGRESQL_MAXIMUM_CONNECTIONS)
        
class _Oracle(_PoolingBroker):
    """
    Implements an Oracle broker.
    """
    _query_mac = """
     SELECT
      m.ip, m.hostname,
      s.gateway, s.subnet_mask, s.broadcast_address, s.domain_name, s.domain_name_servers,
      s.ntp_servers, s.lease_time, s.subnet, s.serial
     FROM maps m, subnets s
     WHERE
      m.mac = :1 AND m.subnet = s.subnet AND m.serial = s.serial
     LIMIT 1
    """

    def __init__(self):
        """
        Constructs the broker.
        """
        self._connection_details = {
         'user': conf.ORACLE_USERNAME,
         'password': conf.ORACLE_PASSWORD,
         'dsn': conf.ORACLE_DATABASE,
        }

        self._setupBroker(conf.ORACLE_MAXIMUM_CONNECTIONS)

class _SQLite(_NonPoolingBroker):
    """
    Implements a SQLite broker.
    """
    _query_mac = """
     SELECT
      m.ip, m.hostname,
      s.gateway, s.subnet_mask, s.broadcast_address, s.domain_name, s.domain_name_servers,
      s.ntp_servers, s.lease_time, s.subnet, s.serial
     FROM maps m, subnets s
     WHERE
      m.mac = ? AND m.subnet = s.subnet AND m.serial = s.serial
     LIMIT 1
    """
    
    def __init__(self):
        """
        Constructs the broker.
        """
        self._connection_details = {
         'database': conf.SQLITE_FILE,
        }
        
        self._setupBroker(1)

class _UppercaseSectionINIFile(object):
    def __init__(self, fname):
        self.name = fname
        self.fp = file(fname)
        
    def readline(self):
        #strip leading spaces
        l = self.fp.readline().lstrip(' ')
        if l and l[0] == "[":
            #uppercase section
            return l.upper()
        return l

class _INIDB(object):

    def __init__(self, *args, **kwargs):
        self._saveHosts()

    def _getCache(self):
        cfg = ConfigParser.ConfigParser(allow_no_value=True)
        cfg.readfp(_UppercaseSectionINIFile(conf.INI_PATH))
        return cfg

    def _saveHosts(self):
        cfg = self._getCache()
        src.logging.writeLog("Updating hosts file")
        if conf.INI_FILE_WRITE_HOSTS_FILE_ALL:
            with open(conf.INI_FILE_WRITE_HOSTS_FILE_ALL, 'w') as f:
                f.write("#DO NOT EDIT THIS FILE\n")
                f.write("#Created automatically by staticDHCPD\n\n")
                for mac in cfg.sections():
                    ip = cfg.get(mac, "ip")
                    hostname = cfg.get(mac, "hostname")
                    if ip and hostname:
                        f.write("%s\t%s\n" % (ip,hostname))
        if conf.INI_FILE_SIGNAL_PID:
            try:
                pid = -1
                if type(conf.INI_FILE_SIGNAL_PID) is int:
                    pid = int(conf.INI_FILE_SIGNAL_PID)
                elif type(conf.INI_FILE_SIGNAL_PID) is str:
                    pid = int(open(conf.INI_FILE_SIGNAL_PID).read().strip())
                if pid < 2:
                    raise Exception("Invalid PID")

                sig = getattr(signal, conf.INI_FILE_SIGNAL_NAME)

                os.kill(pid, sig)                

            except Exception, e:
                src.logging.writeLog("Error sending signal: %s" % e)
                    

    def flushCache(self):
        pass

    def lookupMAC(self, mac):
        cfg = self._getCache()

        mac = mac.upper()

        if not cfg.has_section(mac):
            return None

        ip          = cfg.get(mac,"ip")
        hostname    = cfg.get(mac,"hostname")
        gateway     = cfg.get(mac,"gateway")
        subnet_mask = cfg.get(mac,"subnet_mask")
        broadcast_address = cfg.get(mac, "broadcast_address")
        domain_name = cfg.get(mac, "domain_name")
        domain_name_servers = cfg.get(mac, "domain_name_servers")
        ntp_servers = cfg.get(mac, "ntp_servers")

        lt  = cfg.get(mac, "lease_time")
        if lt is not None and len(lt) > 1:
            if lt[-1] == 's':
                lease_time = int(lt[0:-1])
            elif lt[-1] == 'm':
                lease_time = int(lt[0:-1]) * 60
            elif lt[-1] == 'h':
                lease_time = int(lt[0:-1]) * 60 * 60
            elif lt[-1] == 'd':
                lease_time = int(lt[0:-1]) * 60 * 60 * 24
            else:
                lease_time = int(lt)

        subnet      = cfg.get(mac, "subnet")
        serial      = cfg.getint(mac, "serial")

        if ip and hostname:
            self._saveHosts()

        return ip, hostname, gateway, subnet_mask,\
               broadcast_address, domain_name, domain_name_servers,\
               ntp_servers, lease_time,\
               subnet, serial
        
#Decide which SQL engine to use and store the class in SQL_BROKER
#################################################################
SQL_BROKER = None #: The class of the SQL engine to use.
SQL_MODULE = None #: The module of the SQL engine to use.
if conf.DATABASE_ENGINE == 'MySQL':
    import MySQLdb as SQL_MODULE
    SQL_BROKER = _MySQL
elif conf.DATABASE_ENGINE == 'PostgreSQL':
    import psycopg2 as SQL_MODULE
    SQL_BROKER = _PostgreSQL
elif conf.DATABASE_ENGINE == 'Oracle':
    import cx_Oracle as SQL_MODULE
    SQL_BROKER = _Oracle
elif conf.DATABASE_ENGINE == 'SQLite':
    import sqlite3 as SQL_MODULE
    SQL_BROKER = _SQLite
elif conf.DATABASE_ENGINE == 'INI':
    SQL_BROKER = _INIDB
else:
    raise ValueError("Unknown database engine: %(engine)s" % {
     'engine': conf.DATABASE_ENGINE
    })
    
