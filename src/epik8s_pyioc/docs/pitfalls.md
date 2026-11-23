# Pitfalls of caproto IOCs in epik8s

Each of these was a real bug in a deployed IOC. The project generator (`pyioc init`)
avoids all of them; check hand-written IOCs against this list.

## Enum PVs are strings in putters

caproto passes an enum put to the putter as its **string** (`"Off"`, `"Closed"`) and stores
it as a string afterwards. A value seeded with `write(..., verify_value=False)` stays an
int. Code such as `bool(value)` or `if self.continuous.value:` is then wrong:
`bool("Closed")` is `True` and `"Off"` is truthy. In thorlabscct before 0.2.4, writing
`Shutter=Closed` opened the shutter, and `Continuous` could not be turned off. Compare by
index:

```python
def enum_index(prop, value) -> int:
    return list(prop.enum_strings).index(value) if isinstance(value, str) else int(value)
```

## Arrays shorter than their declared length break Phoebus

Phoebus' CA client (CAJ) throws `BufferUnderflowException` on a waveform shorter than its
`max_length`. The widget stays disconnected (a magenta border) and the plot stays empty;
a char waveform holding `""` fails the same way. Always publish full-length arrays: pad
numbers with NaN (and say how many are valid in a separate PV), and pad text with NUL.

## Strings that operators read

A `pvproperty(value="", max_length=N)` string is a char waveform: a plain `caget` shows
byte codes. Use `dtype=ChannelType.STRING` (at most 39 characters) for identifiers and
short messages, plus a NUL-padded char waveform for the full text.

## One error, logged once

An IOC that polls an absent device logs the same traceback on every poll. Log an error
with its traceback when it changes, at debug level while it repeats, and once more when
the IOC recovers. Do not queue periodic work while the device is disconnected.

## Never block the event loop, never wait forever

Driver calls run with `asyncio.to_thread`. A call that can wait for something external
(a hardware trigger, a long exposure) must be cancellable. Give each such call its own
cancellation handle, bound it with a timeout, and cancel it when a setting changes or an
`Abort` PV is written, instead of making setting writes queue behind it.

## Optional device features must not stop the IOC

An SDK may not implement everything on every model (the Thorlabs virtual device has no
trigger). Apply optional settings at connect without making the connection fail. Report
them in `LastError` only if the configuration asked for them.

## Hostnames

Some vendor libraries take only IP addresses. Resolve a `host` parameter to IPv4 in the
IOC (`socket.getaddrinfo`), so values.yaml can keep the DNS name.

## Runs as uid 1000

The pod's user has no home directory in the image, so `HOME` must be writable
(`pyioc-run` sets `HOME=/tmp`). Nothing may need root at run time.
