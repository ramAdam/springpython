"""
   Copyright 2006-2008 SpringSource (http://springsource.com), All Rights Reserved

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.
"""

# stdlib
import sys
import logging
from threading import RLock
from cStringIO import StringIO
from struct import pack, unpack
from xml.sax.saxutils import escape
from binascii import hexlify, unhexlify
from time import time, mktime, strptime, altzone
from traceback import format_exc

try:
    import cElementTree as etree
except ImportError:
    try:
        import xml.etree.ElementTree as etree
    except ImportError:
        from elementtree import ElementTree as etree

# Spring Python
from springpython.context import DisposableObject
from springpython.jms.core import reserved_attributes, TextMessage
from springpython.util import TRACE1, synchronized
from springpython.jms import (
    JMSException,
    WebSphereMQJMSException,
    NoMessageAvailableException,
    DELIVERY_MODE_NON_PERSISTENT,
    DELIVERY_MODE_PERSISTENT,
)


# Don't pollute the caller's namespace
__all__ = ["WebSphereMQConnectionFactory"]


# Internal constants, don't touch.

# Some WMQ constants are not exposed by pymqi.
_WMQ_MQRFH_VERSION_2 = "\x00\x00\x00\x02"
_WMQ_DEFAULT_ENCODING = 273
_WMQ_DEFAULT_ENCODING_WIRE_FORMAT = pack("!l", _WMQ_DEFAULT_ENCODING)

# 1208 = UTF-8
_WMQ_DEFAULT_CCSID = 1208
_WMQ_DEFAULT_CCSID_WIRE_FORMAT = pack("!l", _WMQ_DEFAULT_CCSID)

# From cmqc.h
_WMQ_MQFMT_RF_HEADER_2 = "MQHRF2  "

# MQRFH_NO_FLAGS_WIRE is in cmqc.h
_WMQ_MQRFH_NO_FLAGS_WIRE_FORMAT = "\x00\x00\x00\x00"

# Java documentation says "214748364.7 seconds".
_WMQ_MAX_EXPIRY_TIME = 214748364.7

_WMQ_ID_PREFIX = "ID:"

# In current implementation, an mcd JMS folder is constant for every message
# sent, so let's build it here.

_mcd = etree.Element("mcd")
_msd = etree.Element("Msd")
_mcd.append(_msd)

# For now, it's always a TextMessage
_msd.text = "jms_text"

_msgbody = etree.Element("msgbody")
_msgbody.set("xmlns:xsi", "dummy")  # We're using a dummy namespace
_msgbody.set("xsi:nil", "true")
_mcd.append(_msgbody)

# Clean up namespace.
del (_msd, _msgbody)


def unhexlify_wmq_id(wmq_id):
    """Converts the WebSphere MQ generated identifier back to bytes,
    i.e. "ID:414d5120535052494e47505954484f4ecc90674a041f0020" -> "AMQ SPRINGPYTHON\xcc\x90gJ\x04\x1f\x00 ".
    """
    return unhexlify(wmq_id.replace(_WMQ_ID_PREFIX, "", 1))


class WebSphereMQConnectionFactory(DisposableObject):
    def __init__(
        self,
        queue_manager=None,
        channel=None,
        host=None,
        listener_port=None,
        cache_open_send_queues=True,
        cache_open_receive_queues=True,
        use_shared_connections=True,
        dynamic_queue_template="SYSTEM.DEFAULT.MODEL.QUEUE",
        ssl=False,
        ssl_cipher_spec=None,
        ssl_key_repository=None,
        needs_mcd=True,
    ):
        self.queue_manager = queue_manager
        self.channel = channel
        self.host = host
        self.listener_port = listener_port

        self.use_shared_connections = use_shared_connections
        self.dynamic_queue_template = dynamic_queue_template

        # SSL support
        self.ssl = ssl
        self.ssl_cipher_spec = ssl_cipher_spec
        self.ssl_key_repository = ssl_key_repository

        # WMQ >= 7.0 must not use the mcd folder
        self.needs_mcd = needs_mcd

        self.logger = logging.getLogger(
            "springpython.jms.factory.WebSphereMQConnectionFactory"
        )

        import CMQC
        import pymqi

        self.CMQC = CMQC
        self.mq = pymqi

        self._open_send_queues_cache = {}
        self._open_receive_queues_cache = {}
        self._open_dynamic_queues_cache = {}

        self.cache_open_send_queues = cache_open_send_queues
        self.cache_open_receive_queues = cache_open_receive_queues

        self._is_connected = False
        self._disconnecting = False

        self.logger.log(TRACE1, "Finished __init__")

    @synchronized()
    def destroy(self):
        if self._is_connected:
            self._disconnecting = True
            try:
                self.logger.info("Deleting queues from caches")
                self._open_send_queues_cache.clear()
                self._open_receive_queues_cache.clear()
                self._open_dynamic_queues_cache.clear()
                self.logger.info("Caches cleared")
            except Exception as e:
                try:
                    self.logger.error(
                        "Could not clear the caches. Exception [%s]" % format_exc()
                    )
                except:
                    pass
            try:
                self.logger.info(
                    "Disconnecting from queue manager [%s]" % self.queue_manager
                )
                self.mgr.disconnect()
                self.logger.info(
                    "Disconnected from queue manager [%s]" % self.queue_manager
                )
            except Exception as e:
                try:
                    self.logger.error(
                        "Could not disconnect from queue manager [%s], exception [%s] "
                        % (self.queue_manager, format_exc())
                    )
                except Exception:
                    pass

            self._is_connected = False

        else:
            self.logger.debug("Not connected, skipping cleaning up the resources")

    def get_connection_info(self):
        return "queue manager=[%s], channel=[%s], conn_name=[%s(%s)]" % (
            self.queue_manager,
            self.channel,
            self.host,
            self.listener_port,
        )

    @synchronized()
    def _connect(self):
        if self._is_connected:
            return

        conn_name = "%s(%s)" % (self.host, self.listener_port)

        self.logger.info(
            "Connecting to queue manager [%s], channel [%s]"
            ", connection info [%s]" % (self.queue_manager, self.channel, conn_name)
        )
        self.mgr = self.mq.QueueManager(None)

        sco = self.mq.sco()
        cd = self.mq.cd()
        cd.ChannelName = self.channel
        cd.ConnectionName = conn_name
        cd.ChannelType = self.CMQC.MQCHT_CLNTCONN
        cd.TransportType = self.CMQC.MQXPT_TCP

        if self.ssl:
            if not (self.ssl_cipher_spec and self.ssl_key_repository):
                msg = "SSL support requires setting both ssl_cipher_spec and ssl_key_repository"
                self.logger.error(msg)
                raise JMSException(msg)

            sco.KeyRepository = self.ssl_key_repository
            cd.SSLCipherSpec = self.ssl_cipher_spec

        if self.use_shared_connections:
            connect_options = self.CMQC.MQCNO_HANDLE_SHARE_BLOCK
        else:
            connect_options = self.CMQC.MQCNO_HANDLE_SHARE_NONE

        try:
            self.mgr.connectWithOptions(
                self.queue_manager, cd=cd, opts=connect_options, sco=sco
            )
        except self.mq.MQMIError as e:
            exc = WebSphereMQJMSException(e, e.comp, e.reason)
            raise exc
        except Exception as e:
            self.logger.error("Could not connect to queue manager, e=[%s]" % e)
            exc = WebSphereMQJMSException(e, None, None)
            raise exc
        else:
            self._is_connected = True
            self.logger.info(
                "Successfully connected to queue manager [%s]"
                ", channel [%s], connection info [%s]"
                % (self.queue_manager, self.channel, conn_name)
            )

    def _get_queue_from_cache(self, destination, cache):
        lock = RLock()
        lock.acquire()
        try:
            # Will usually choose this path and find the queue here.
            if destination in cache:
                return cache[destination]
            else:
                self.logger.debug("Adding queue [%s] to the cache" % destination)
                cache[destination] = self.mq.Queue(
                    self.mgr,
                    destination,
                    self.CMQC.MQOO_INPUT_SHARED | self.CMQC.MQOO_OUTPUT,
                )
                self.logger.debug("Queue [%s] added to the cache" % destination)
                self.logger.log(TRACE1, "Cache contents [%s]" % cache)
                return cache[destination]
        finally:
            lock.release()

    def get_queue_for_sending(self, destination):
        if self.cache_open_send_queues:
            queue = self._get_queue_from_cache(
                destination, self._open_send_queues_cache
            )
        else:
            queue = self.mq.Queue(self.mgr, destination)

        return queue

    def get_queue_for_receiving(self, destination):
        if self.cache_open_receive_queues:
            queue = self._get_queue_from_cache(
                destination, self._open_receive_queues_cache
            )
        else:
            queue = self.mq.Queue(self.mgr, destination)

        return queue

    def send(self, message, destination):
        if self._disconnecting:
            self.logger.info("Connection factory disconnecting, aborting receive")
            return
        else:
            self.logger.log(TRACE1, "send -> not disconnecting")

        if not self._is_connected:
            self.logger.log(TRACE1, "send -> _is_connected1 %s" % self._is_connected)
            self._connect()
            self.logger.log(TRACE1, "send -> _is_connected2 %s" % self._is_connected)

        destination = self._strip_prefixes_from_destination(destination)

        # Will consist of an MQRFH2 header and the actual business payload.
        buff = StringIO()

        # Build the message descriptor (MQMD)
        md = self._build_md(message)

        # Create MQRFH2 header
        now = long(time() * 1000)
        mqrfh2jms = MQRFH2JMS(self.needs_mcd).build_header(
            message, destination, self.CMQC, now
        )

        buff.write(mqrfh2jms)
        if message.text != None:
            buff.write(message.text.encode("utf-8"))

        body = buff.getvalue()
        buff.close()

        queue = self.get_queue_for_sending(destination)

        try:
            queue.put(body, md)
        except self.mq.MQMIError as e:
            self.logger.error(
                "MQMIError in queue.put, e.comp [%s], e.reason [%s] "
                % (e.comp, e.reason)
            )
            exc = WebSphereMQJMSException(e, e.comp, e.reason)
            raise exc

        if not self.cache_open_send_queues:
            queue.close()

        # Map the JMS headers overwritten by calling queue.put
        message.jms_message_id = _WMQ_ID_PREFIX + hexlify(md.MsgId)
        message.jms_priority = md.Priority
        message.jms_correlation_id = _WMQ_ID_PREFIX + hexlify(md.CorrelId)
        message.JMSXUserID = md.UserIdentifier
        message.JMSXAppID = md.PutApplName

        if md.PutDate and md.PutTime:
            message.jms_timestamp = self._get_jms_timestamp_from_md(
                md.PutDate.strip(), md.PutTime.strip()
            )
            message.JMS_IBM_PutDate = md.PutDate.strip()
            message.JMS_IBM_PutTime = md.PutTime.strip()
        else:
            self.logger.warning(
                "No md.PutDate and md.PutTime found, md [%r]" % repr(md)
            )

        # queue.put has succeeded, so overwrite expiration time as well
        if message.jms_expiration != None:
            message.jms_expiration += now

        self.logger.debug(
            "Successfully sent a message [%s], connection info [%s]"
            % (message, self.get_connection_info())
        )

        self.logger.log(
            TRACE1, "message [%s], body [%r], md [%r]" % (message, body, repr(md))
        )

    def receive(self, destination, wait_interval):
        if self._disconnecting:
            self.logger.info("Connection factory disconnecting, aborting receive")
            return
        else:
            self.logger.log(TRACE1, "receive -> not disconnecting")

        if not self._is_connected:
            self.logger.log(TRACE1, "receive -> _is_connected1 %s" % self._is_connected)
            self._connect()
            self.logger.log(TRACE1, "receive -> _is_connected2 %s" % self._is_connected)

        queue = self.get_queue_for_receiving(destination)

        try:
            # Default message descriptor ..
            md = self.mq.md()

            # .. and custom get message options
            gmo = self.mq.gmo()
            gmo.Options = self.CMQC.MQGMO_WAIT | self.CMQC.MQGMO_FAIL_IF_QUIESCING
            gmo.WaitInterval = wait_interval

            message = queue.get(None, md, gmo)

            return self._build_text_message(md, message)

        except self.mq.MQMIError as e:
            if e.reason == self.CMQC.MQRC_NO_MSG_AVAILABLE:
                text = (
                    "No message available for destination [%s], "
                    "wait_interval [%s] ms" % (destination, wait_interval)
                )
                raise NoMessageAvailableException(text)
            else:
                self.logger.log(
                    TRACE1,
                    "Exception caught in get, e.comp=[%s], e.reason=[%s]"
                    % (e.comp, e.reason),
                )
                exc = WebSphereMQJMSException(e, e.comp, e.reason)
                raise exc

    def open_dynamic_queue(self):
        if self._disconnecting:
            self.logger.info(
                "Connection factory disconnecting, aborting open_dynamic_queue"
            )
            return
        else:
            self.logger.log(TRACE1, "open_dynamic_queue -> not disconnecting")

        if not self._is_connected:
            self.logger.log(
                TRACE1, "open_dynamic_queue -> _is_connected1 %s" % self._is_connected
            )
            self._connect()
            self.logger.log(
                TRACE1, "open_dynamic_queue -> _is_connected2 %s" % self._is_connected
            )

        dynamic_queue = self.mq.Queue(
            self.mgr, self.dynamic_queue_template, self.CMQC.MQOO_INPUT_SHARED
        )

        # A bit hackish, but there's no other way to get its name.
        dynamic_queue_name = dynamic_queue._Queue__qDesc.ObjectName.strip()

        lock = RLock()
        lock.acquire()
        try:
            self._open_dynamic_queues_cache[dynamic_queue_name] = dynamic_queue
        finally:
            lock.release()

        self.logger.log(
            TRACE1,
            "Successfully created a dynamic queue, descriptor [%s]"
            % (dynamic_queue._Queue__qDesc),
        )

        return dynamic_queue_name

    def close_dynamic_queue(self, dynamic_queue_name):
        if self._disconnecting:
            self.logger.info(
                "Connection factory disconnecting, aborting close_dynamic_queue"
            )
            return
        else:
            self.logger.log(TRACE1, "close_dynamic_queue -> not disconnecting")

        if not self._is_connected:
            # If we're not connected then all dynamic queues had been already closed.
            self.logger.log(
                TRACE1, "close_dynamic_queue -> _is_connected1 %s" % self._is_connected
            )
            return
        else:
            self.logger.log(
                TRACE1, "close_dynamic_queue -> _is_connected2 %s" % self._is_connected
            )
            lock = RLock()
            lock.acquire()
            try:
                dynamic_queue = self._open_dynamic_queues_cache[dynamic_queue_name]
                dynamic_queue.close()

                self._open_dynamic_queues_cache.pop(dynamic_queue_name, None)
                self._open_send_queues_cache.pop(dynamic_queue_name, None)
                self._open_receive_queues_cache.pop(dynamic_queue_name, None)

                self.logger.log(
                    TRACE1,
                    "Successfully closed a dynamic queue [%s]" % (dynamic_queue_name),
                )

            finally:
                lock.release()

    def _get_jms_timestamp_from_md(self, put_date, put_time):
        pattern = "%Y%m%d%H%M%S"
        centi = int(put_time[6:]) / 100.0

        strp = strptime(put_date + put_time[:6], pattern)
        mk = mktime(strp)

        return long((mk - altzone + centi) * 1000.0)

    def _build_text_message(self, md, message):
        self.logger.log(
            TRACE1, "Building a text message [%r], md [%r]" % (repr(message), repr(md))
        )

        mqrfh2 = MQRFH2JMS(self.needs_mcd)
        mqrfh2.build_folders_and_payload_from_message(message)

        jms_folder = mqrfh2.folders.get("jms", None)
        mcd_folder = mqrfh2.folders.get("mcd", None)
        usr_folder = mqrfh2.folders.get("usr", None)

        # Create a message instance ..
        text_message = TextMessage()

        if usr_folder:
            for attr_name, attr_value in usr_folder.items():
                setattr(text_message, attr_name, str(attr_value))

        # .. set its JMS properties ..

        if jms_folder:
            if jms_folder.find("Dst") is not None:
                text_message.jms_destination = jms_folder.find("Dst").text.strip()

            if jms_folder.find("Exp") is not None:
                text_message.jms_expiration = long(jms_folder.find("Exp").text)
            else:
                text_message.jms_expiration = 0  # Same as in Java

            if jms_folder.find("Cid") is not None:
                text_message.jms_correlation_id = jms_folder.find("Cid").text

        if md.Persistence == self.CMQC.MQPER_NOT_PERSISTENT:
            text_message.jms_delivery_mode = DELIVERY_MODE_NON_PERSISTENT
        elif md.Persistence in (
            self.CMQC.MQPER_PERSISTENT,
            self.CMQC.MQPER_PERSISTENCE_AS_Q_DEF,
        ):
            text_message.jms_delivery_mode = DELIVERY_MODE_PERSISTENT
        else:
            text = "Don't know how to handle md.Persistence mode [%s]" % (
                md.Persistence
            )
            self.logger.error(text)
            exc = WebSphereMQJMSException(text)
            raise exc

        if md.ReplyToQ.strip():
            self.logger.log(TRACE1, "Found md.ReplyToQ=[%r]" % md.ReplyToQ)
            text_message.jms_reply_to = (
                "queue://" + md.ReplyToQMgr.strip() + "/" + md.ReplyToQ.strip()
            )

        text_message.jms_priority = md.Priority
        text_message.jms_message_id = _WMQ_ID_PREFIX + hexlify(md.MsgId)
        text_message.jms_timestamp = self._get_jms_timestamp_from_md(
            md.PutDate.strip(), md.PutTime.strip()
        )
        text_message.jms_redelivered = bool(int(md.BackoutCount))

        text_message.JMSXUserID = md.UserIdentifier.strip()
        text_message.JMSXAppID = md.PutApplName.strip()
        text_message.JMSXDeliveryCount = md.BackoutCount
        text_message.JMSXGroupID = md.GroupId.strip()
        text_message.JMSXGroupSeq = md.MsgSeqNumber

        md_report_to_jms = {
            self.CMQC.MQRO_EXCEPTION: "Exception",
            self.CMQC.MQRO_EXPIRATION: "Expiration",
            self.CMQC.MQRO_COA: "COA",
            self.CMQC.MQRO_COD: "COD",
            self.CMQC.MQRO_PAN: "PAN",
            self.CMQC.MQRO_NAN: "NAN",
            self.CMQC.MQRO_PASS_MSG_ID: "Pass_Msg_ID",
            self.CMQC.MQRO_PASS_CORREL_ID: "Pass_Correl_ID",
            self.CMQC.MQRO_DISCARD_MSG: "Discard_Msg",
        }

        for report_name, jms_header_name in md_report_to_jms.items():
            report_value = md.Report & report_name
            if report_value:
                header_value = report_value
            else:
                header_value = None

            setattr(text_message, "JMS_IBM_Report_" + jms_header_name, header_value)

        text_message.JMS_IBM_MsgType = md.MsgType
        text_message.JMS_IBM_Feedback = md.Feedback
        text_message.JMS_IBM_Format = md.Format.strip()
        text_message.JMS_IBM_PutApplType = md.PutApplType
        text_message.JMS_IBM_PutDate = md.PutDate.strip()
        text_message.JMS_IBM_PutTime = md.PutTime.strip()

        if md.MsgFlags & self.CMQC.MQMF_LAST_MSG_IN_GROUP:
            text_message.JMS_IBM_Last_Msg_In_Group = self.CMQC.MQMF_LAST_MSG_IN_GROUP
        else:
            text_message.JMS_IBM_Last_Msg_In_Group = None

        # .. and its payload too.
        if mqrfh2.payload:
            text_message.text = mqrfh2.payload

        return text_message

    def _strip_prefixes_from_destination(self, destination):
        if destination.startswith("queue:///"):
            return destination.replace("queue:///", "", 1)
        elif destination.startswith("queue://"):
            no_qm_dest = destination.replace("queue://", "", 1)
            no_qm_dest = no_qm_dest.split("/")[1:]
            return "/".join(no_qm_dest)
        else:
            return destination

    def _build_md(self, message):
        md = self.mq.md()

        md.Format = _WMQ_MQFMT_RF_HEADER_2
        md.CodedCharSetId = _WMQ_DEFAULT_CCSID
        md.Encoding = _WMQ_DEFAULT_ENCODING

        # Map JMS headers to MQMD

        if message.jms_correlation_id:
            if message.jms_correlation_id.startswith(_WMQ_ID_PREFIX):
                md.CorrelId = unhexlify_wmq_id(message.jms_correlation_id)
            else:
                md.CorrelId = message.jms_correlation_id.ljust(24)[:24]

        if message.jms_delivery_mode:

            if message.jms_delivery_mode == DELIVERY_MODE_NON_PERSISTENT:
                persistence = self.CMQC.MQPER_NOT_PERSISTENT
            elif message.jms_delivery_mode == DELIVERY_MODE_PERSISTENT:
                persistence = self.CMQC.MQPER_PERSISTENT
            else:
                info = (
                    "jms_delivery_mode should be equal to DELIVERY_MODE_NON_PERSISTENT or DELIVERY_MODE_PERSISTENT, not [%s]"
                    % message.jms_delivery_mode
                )
                self.logger.error(info)
                exc = JMSException(info)
                raise exc

            md.Persistence = persistence

        if message.jms_priority:
            md.Priority = message.jms_priority

        if message.jms_reply_to:
            md.ReplyToQ = message.jms_reply_to

            self.logger.log(
                TRACE1,
                (
                    "Set jms_reply_to. md.ReplyToQ=[%r],"
                    " message.jms_reply_to=[%r]" % (md.ReplyToQ, message.jms_reply_to)
                ),
            )

        # jms_expiration is in milliseconds, md.Expiry is in centiseconds.
        if message.jms_expiration:
            if message.jms_expiration / 1000 > _WMQ_MAX_EXPIRY_TIME:
                md.Expiry = self.CMQC.MQEI_UNLIMITED
            else:
                md.Expiry = message.jms_expiration / 10

        # WebSphere MQ provider-specific JMS headers

        jmsxgroupseq = getattr(message, "JMSXGroupSeq", None)
        if jmsxgroupseq != None:
            md.MsgSeqNumber = jmsxgroupseq
            md.MsgFlags |= self.CMQC.MQMF_MSG_IN_GROUP

        jmsxgroupid = getattr(message, "JMSXGroupID", None)
        if jmsxgroupid != None:
            if jmsxgroupid.startswith(_WMQ_ID_PREFIX):
                md.GroupId = unhexlify_wmq_id(jmsxgroupid)
            else:
                md.GroupId = jmsxgroupid.ljust(24)[:24]
            md.MsgFlags |= self.CMQC.MQMF_MSG_IN_GROUP

        for report_name in (
            "Exception",
            "Expiration",
            "COA",
            "COD",
            "PAN",
            "NAN",
            "Pass_Msg_ID",
            "Pass_Correl_ID",
            "Discard_Msg",
        ):

            report = getattr(message, "JMS_IBM_Report_" + report_name, None)
            if report != None:
                md.Report |= report

        # Doesn't make much sense to map feedback options as we're stuffed into
        # request messages (MQMT_REQUEST) not report messages (MQMT_REPORT)
        # but different types of messages are still possible to implement in
        # the future so let's leave it.

        jms_ibm_feedback = getattr(message, "JMS_IBM_Feedback", None)
        if jms_ibm_feedback != None:
            md.Feedback = jms_ibm_feedback

        jms_ibm_last_msg_in_group = getattr(message, "JMS_IBM_Last_Msg_In_Group", None)
        if jms_ibm_last_msg_in_group != None:
            md.MsgFlags |= self.CMQC.MQMF_LAST_MSG_IN_GROUP

        return md


class MQRFH2JMS(object):
    """A class for representing a subset of MQRFH2, suitable for passing
    WebSphere MQ JMS headers around.
    """

    # 4 bytes - MQRFH_STRUC_ID
    # 4 bytes - _WMQ_MQRFH_VERSION_2
    # 4 bytes - the whole MQRFH2 header length
    # 4 bytes - Encoding
    # 4 bytes - CodedCharacterSetId
    # 8 bytes - MQFMT_STRING
    # 4 bytes - MQRFH_NO_FLAGS
    # 4 bytes - NameValueCCSID
    FIXED_PART_LENGTH = 36

    # MQRFH2 folder length must be a multiple of 4.
    FOLDER_LENGTH_MULTIPLE = 4

    # Size of a folder header is always 4 bytes.
    FOLDER_SIZE_HEADER_LENGTH = 4

    def __init__(self, needs_mcd=True):

        # Whether to add the mcd folder. Needs to be False for everything to
        # work properly with WMQ >= 7.0
        self.needs_mcd = needs_mcd

        self.folders = {}
        self.payload = None

        self.logger = logging.getLogger("springpython.jms.factory.MQRFH2JMS")

    def _pad_folder(self, folder):
        """Pads the folder to a multiple of 4, as required by WebSphere MQ."""
        folder_len = len(folder)

        if folder_len % MQRFH2JMS.FOLDER_LENGTH_MULTIPLE == 0:
            return folder
        else:
            padding = (
                MQRFH2JMS.FOLDER_LENGTH_MULTIPLE
                - folder_len % MQRFH2JMS.FOLDER_LENGTH_MULTIPLE
            )
            return folder.ljust(folder_len + padding)

    def build_folders_and_payload_from_message(self, message):
        total_mqrfh2_length = unpack("!l", message[8:12])[0]

        mqrfh2 = message[MQRFH2JMS.FIXED_PART_LENGTH : total_mqrfh2_length]
        self.payload = message[MQRFH2JMS.FIXED_PART_LENGTH + len(mqrfh2) :]

        self.logger.log(TRACE1, "message [%r]" % message)
        self.logger.log(TRACE1, "mqrfh2 [%r]" % mqrfh2)
        self.logger.log(TRACE1, "self.payload [%r]" % self.payload)

        left = mqrfh2
        while left:
            current_folder_length = unpack("!l", left[:4])[0]
            raw_folder = left[
                MQRFH2JMS.FOLDER_SIZE_HEADER_LENGTH : MQRFH2JMS.FOLDER_SIZE_HEADER_LENGTH
                + current_folder_length
            ]

            self.logger.log(TRACE1, "raw_folder [%r]" % raw_folder)
            self.build_folder(raw_folder)

            left = left[MQRFH2JMS.FOLDER_SIZE_HEADER_LENGTH + current_folder_length :]

    def build_folder(self, raw_folder):

        # Java JMS sends folders with unbound prefixes, i.e. <msgbody xsi:nil="true"></msgbody>
        # which is in no way a valid XML so we have to insert the prefix ourselves
        # in order to avoid parser bailing out with an ExpatError. I can't think
        # of any other way to work around it if we'd like to treat folders as
        # XML(-like) structures.

        if 'xsi:nil="true"' in raw_folder and not "xmlns" in raw_folder:
            self.logger.log(
                TRACE1, "Binding xsi:nil to a dummy namespace [%s]" % raw_folder
            )
            raw_folder = raw_folder.replace(
                'xsi:nil="true"', 'xmlns:xsi="dummy" xsi:nil="true"'
            )
            self.logger.log(TRACE1, "raw_folder after binding [%s]" % raw_folder)

        folder = etree.fromstring(raw_folder)
        root_name = folder.tag

        root_names = ["jms", "usr"]
        if self.needs_mcd:
            root_names.append("mcd")

        if root_name in root_names:
            self.folders[root_name] = folder
        else:
            self.logger.warn(
                "Ignoring unrecognized JMS folder [%s]=[%s]" % (root_name, raw_folder)
            )

    def build_header(self, message, queue_name, CMQC, now):

        if self.needs_mcd:
            self.folders["mcd"] = _mcd
            mcd = self._pad_folder(etree.tostring(self.folders["mcd"]))
            mcd_len = len(mcd)
        else:
            mcd_len = 0

        self.add_jms(message, queue_name, now)
        self.add_usr(message)

        jms = self._pad_folder(etree.tostring(self.folders["jms"]))

        if "usr" in self.folders:
            usr = self._pad_folder(etree.tostring(self.folders["usr"]))
            usr_len = len(usr)
        else:
            usr_len = 0

        jms_len = len(jms)

        total_header_length = 0
        total_header_length += MQRFH2JMS.FIXED_PART_LENGTH

        # Each folder has a 4-byte header describing its length,
        # hence the "len(self.folders) * 4" below.
        variable_part_length = len(self.folders) * 4 + mcd_len + jms_len + usr_len

        total_header_length += variable_part_length

        buff = StringIO()

        buff.write(CMQC.MQRFH_STRUC_ID)
        buff.write(_WMQ_MQRFH_VERSION_2)
        buff.write(pack("!l", total_header_length))
        buff.write(_WMQ_DEFAULT_ENCODING_WIRE_FORMAT)
        buff.write(_WMQ_DEFAULT_CCSID_WIRE_FORMAT)
        buff.write(CMQC.MQFMT_STRING)
        buff.write(_WMQ_MQRFH_NO_FLAGS_WIRE_FORMAT)
        buff.write(_WMQ_DEFAULT_CCSID_WIRE_FORMAT)

        if self.needs_mcd:
            buff.write(pack("!l", mcd_len))
            buff.write(mcd)

        buff.write(pack("!l", jms_len))
        buff.write(jms)

        if "usr" in self.folders:
            buff.write(pack("!l", usr_len))
            buff.write(usr)

        value = buff.getvalue()
        buff.close()

        return value

    def add_jms(self, message, queue_name, now):

        jms = etree.Element("jms")
        dst = etree.Element("Dst")
        tms = etree.Element("Tms")
        dlv = etree.Element("Dlv")

        jms.append(dst)
        jms.append(tms)
        jms.append(dlv)

        tms.text = unicode(now)
        dst.text = "queue:///" + queue_name
        dlv.text = unicode(message.jms_delivery_mode)

        if message.jms_expiration:
            exp = etree.Element("Exp")
            exp.text = unicode(now + message.jms_expiration)
            self.logger.log(TRACE1, "jms.Exp [%r]" % exp.text)
            jms.append(exp)

        if message.jms_priority:
            pri = etree.Element("Pri")
            pri.text = unicode(message.jms_priority)
            self.logger.log(TRACE1, "jms.Pri [%r]" % pri.text)
            jms.append(pri)

        if message.jms_correlation_id:
            cid = etree.Element("Cid")
            cid.text = unicode(message.jms_correlation_id)
            self.logger.log(TRACE1, "jms.Cid [%r]" % cid.text)
            jms.append(cid)

        self.folders["jms"] = jms

    def add_usr(self, message):
        user_attrs = set(dir(message)) - reserved_attributes
        self.logger.log(TRACE1, "user_attrs [%s]" % user_attrs)

        if user_attrs:
            usr = etree.Element("usr")

            for user_attr in user_attrs:

                user_attr_value = getattr(message, user_attr)

                # Some values are integers, e.g. delivery_mode
                if isinstance(user_attr_value, basestring):
                    user_attr_value = escape(user_attr_value)

                # Create a JMS attribute and set its value.
                user_attr = etree.Element(unicode(user_attr))
                user_attr.text = unicode(user_attr_value)
                usr.append(user_attr)

            self.folders["usr"] = usr
