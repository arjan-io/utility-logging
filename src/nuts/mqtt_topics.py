"""One MQTT topic map for both the publisher and the PV collector."""

import os


PUBLISHER_SUFFIXES = ("dsmr/electricity", "dsmr/kosten", "dsmr/gas", "water")
SUFFIXES = PUBLISHER_SUFFIXES + ("pv",)


def topic_targets(suffix, prefix, compat_prefix=""):
    if suffix not in SUFFIXES:
        raise ValueError("Onbekend MQTT-onderwerp: {}".format(suffix))
    prefix = prefix.strip().rstrip("/")
    compat_prefix = compat_prefix.strip().rstrip("/")
    if not prefix:
        raise ValueError("MQTT_PREFIX mag niet leeg zijn")
    primary = prefix + "/" + suffix
    secondary = compat_prefix + "/" + suffix if compat_prefix else None
    return (primary, secondary) if secondary and secondary != primary else (primary,)


def configured_topics(suffix):
    return topic_targets(suffix, os.getenv("MQTT_PREFIX", "sensors"),
                         os.getenv("MQTT_COMPAT_PREFIX", ""))
