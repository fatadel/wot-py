#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
WoT application that consumes the benchmark Thing
and analyzes the captured packet traces.
"""

import argparse
import asyncio
import logging
import os
import pprint
import tempfile
import uuid
from subprocess import Popen, PIPE, TimeoutExpired
from urllib.parse import urlparse

import pyshark

from wotpy.protocols.enums import Protocols
from wotpy.protocols.http.client import HTTPClient
from wotpy.protocols.ws.client import WebsocketClient
from wotpy.wot.servient import Servient
from wotpy.wot.wot import WoT

LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
logging.basicConfig(format=LOG_FORMAT)
logger = logging.getLogger()
logger.setLevel(logging.INFO)


class ConsumedThingCapture(object):
    """Represents a set of packets that have been captured
    from the network interactions with a remote ConsumedThing."""

    START_STOP_WINDOW_SECS = 2.0
    PROCESS_LOOP_SLEEP = 0.1
    PROCESS_TIMEOUT = 0.01

    def __init__(self, consumed_thing):
        self._process = None
        self._output_file = None
        self._consumed_thing = consumed_thing

    def _forms_generator(self):
        """Generator that yields every Form in the ConsumedThing."""

        intrct_dicts = [
            self._consumed_thing.properties,
            self._consumed_thing.actions,
            self._consumed_thing.events
        ]

        for intrct_dict in intrct_dicts:
            for name in intrct_dict:
                for form in intrct_dict[name].forms:
                    yield form

    def _get_capture_hosts(self):
        """Returns the list of hosts that are listed in the ConsumedThing Forms."""

        hosts = set()

        for form in self._forms_generator():
            hosts.add(urlparse(form.href).hostname)

        logger.info("Hosts extracted from {}: {}".format(
            self._consumed_thing,
            pprint.pformat(hosts)))

        return list(hosts)

    async def start(self, iface):
        """Starts capturing packets for the ConsumedThing
        hosts in the given network interface."""

        assert not self._process
        assert not self._output_file

        self._output_file = os.path.join(
            tempfile.gettempdir(),
            "{}.pcapng".format(uuid.uuid4().hex))

        filter_host = " or ".join([
            "(host {})".format(item)
            for item in self._get_capture_hosts()
        ])

        command = "tshark -i {} -F pcapng -w {} -f \"{}\"".format(
            iface,
            self._output_file,
            filter_host)

        logger.info("Running capture process: {}".format(command))

        self._process = Popen(command, stdout=PIPE, stderr=PIPE, shell=True)

        await asyncio.sleep(self.START_STOP_WINDOW_SECS)

    async def stop(self):
        """Stops the capture process that is currently in progress."""

        assert self._process
        assert self._output_file

        await asyncio.sleep(self.START_STOP_WINDOW_SECS)

        logger.info("Terminating process: {}".format(self._process))

        self._process.terminate()

        while True:
            try:
                stdout, stderr = self._process.communicate(timeout=self.PROCESS_TIMEOUT)
                break
            except TimeoutExpired:
                pass

            await asyncio.sleep(self.PROCESS_LOOP_SLEEP)

        logger.info("Terminated:\n\tstdout:: {}\n\tstderr:: {}".format(stdout, stderr))

        self._process = None

    def _remove_output_file(self):
        """Removes the temp pcapng file that contains the captured packets."""

        if not self._output_file:
            return

        logger.info("Removing temp capture file: {}".format(self._output_file))

        os.remove(self._output_file)

    def clear(self):
        """Cleans the internal state to enable this
        instance to capture another set of packets."""

        self._remove_output_file()
        self._process = None
        self._output_file = None

    def _build_display_filter(self, protocol):
        """Returns the Wireshark display filter for packets of the given protocol."""

        default_ports = {
            Protocols.HTTP: 80,
            Protocols.WEBSOCKETS: 81,
            Protocols.MQTT: 1883,
            Protocols.COAP: 5683
        }

        transports = {
            Protocols.HTTP: "tcp",
            Protocols.WEBSOCKETS: "tcp",
            Protocols.MQTT: "tcp",
            Protocols.COAP: "udp"
        }

        schemes = {
            Protocols.HTTP: ["http", "https"],
            Protocols.WEBSOCKETS: ["ws", "wss"],
            Protocols.MQTT: ["mqtt", "mqtts"],
            Protocols.COAP: ["coap", "coaps"]
        }

        protocol_forms = [
            form for form in self._forms_generator()
            if urlparse(form.href).scheme in schemes[protocol]
        ]

        ports = {
            urlparse(form.href).port if urlparse(form.href).port else default_ports[protocol]
            for form in protocol_forms
        }

        display_filter = " or ".join([
            "{}.port == {}".format(transports[protocol], port)
            for port in ports
        ])

        logger.info("Display filter ({}): {}".format(protocol, display_filter))

        return display_filter

    def filter_packets(self, protocol):
        """Returns the set of captured packets that match the given protocol."""

        assert not self._process
        assert self._output_file

        display_filter = self._build_display_filter(protocol)

        return pyshark.FileCapture(self._output_file, display_filter=display_filter)

    def get_capture_size(self, protocol):
        """Returns the total size (bytes) of the captured packets for the given protocol."""

        pyshark_cap = self.filter_packets(protocol)
        size = sum([int(pkt.length) for pkt in pyshark_cap])
        pyshark_cap.close()

        return size


def build_protocol_client(protocol):
    """Factory function to build the protocol client for the given protocol."""

    if protocol == Protocols.HTTP:
        return HTTPClient()
    elif protocol == Protocols.WEBSOCKETS:
        return WebsocketClient()
    elif protocol == Protocols.COAP:
        from wotpy.protocols.coap.client import CoAPClient
        return CoAPClient()
    elif protocol == Protocols.MQTT:
        from wotpy.protocols.mqtt.client import MQTTClient
        return MQTTClient()


async def consume(td_url, protocol):
    """Gets the remote Thing Description and returns a ConsumedThing."""

    clients = [build_protocol_client(protocol)]
    wot = WoT(servient=Servient(clients=clients))
    consumed_thing = await wot.consume_from_url(td_url)
    logger.info("ConsumedThing: {}".format(consumed_thing))

    return consumed_thing


async def consume_event_burst(consumed_thing, iface, sub_sleep=1.0, **kwargs):
    """Invokes the action to initiate an event burst and subscribes to those events."""

    cap = ConsumedThingCapture(consumed_thing)
    await cap.start(iface)

    burst_id = uuid.uuid4().hex
    done = asyncio.Future()

    def on_next(item):
        if item.data.get("id") != burst_id:
            return

        logger.info("Next :: {}".format(item))
        item.data.get("burstEnd", False) and not done.done() and done.set_result(True)

    subscription = consumed_thing.events["burstEvent"].subscribe(
        on_next=on_next,
        on_completed=lambda: logger.info("Completed"),
        on_error=lambda error: logger.warning("Error :: {}".format(error)))

    await asyncio.sleep(sub_sleep)

    invoke_arg = {"id": burst_id}
    invoke_arg.update(kwargs)

    action_result = consumed_thing.actions["startEventBurst"].invoke(invoke_arg)

    await asyncio.wait([
        done,
        action_result
    ])

    logger.info("Subscription disposal")

    subscription.dispose()

    await cap.stop()

    return cap


async def consume_round_trip_action(consumed_thing, iface, total=10):
    """Invokes the action to check the round trip time."""

    cap = ConsumedThingCapture(consumed_thing)
    await cap.start(iface)

    action_results = await asyncio.wait([
        consumed_thing.actions["measureRoundTrip"].invoke()
        for _ in range(total)
    ])

    logger.info(pprint.pformat(action_results))

    await cap.stop()

    return cap


def parse_args():
    """Parses and returns the command line arguments."""

    parser = argparse.ArgumentParser(description="Benchmark Thing WoT client")

    parser.add_argument(
        "--url",
        dest="td_url",
        required=True,
        help="Benchmark Thing Description URL")

    parser.add_argument(
        "--iface",
        dest="capture_iface",
        required=True,
        help="Network interface to capture packages from")

    parser.add_argument(
        "--protocol",
        dest="protocol",
        required=True,
        choices=Protocols.list(),
        help="Protocol binding that should be used by the WoT client")

    return parser.parse_args()


def main():
    """Main entrypoint."""

    args = parse_args()

    logger.info("Arguments:\n{}".format(pprint.pformat(vars(args))))

    loop = asyncio.get_event_loop()
    consumed_thing = loop.run_until_complete(consume(args.td_url, args.protocol))

    logger.info("Consuming event burst")

    cap_burst = loop.run_until_complete(
        consume_event_burst(consumed_thing, args.capture_iface))

    logger.info("Size: {} bytes".format(cap_burst.get_capture_size(args.protocol)))


if __name__ == "__main__":
    main()
