# -*- coding: utf-8 -*-
"""Driver for FreeStyle Lite devices.

Supported features:
    - get readings (ignores ketone results);
    - use the glucose unit preset on the device by default;
    - get and set date and time;
    - get serial number and software version.

Expected device path: /dev/ttyUSB0 or similar serial port device.

Further information on the device protocol can be found at

http://www.flupzor.nl/protocol.html
"""

__author__ = 'Diego Elio Pettenò, William Garber'
__email__ = 'flameeyes@flameeyes.eu, william.garber@att.net'
__copyright__ = 'Copyright © 2016-2017, Diego Elio Pettenò'
__license__ = 'MIT'

import datetime
import logging
import re

from glucometerutils import common
from glucometerutils import exceptions
from glucometerutils.support import serial


_CLOCK_RE = re.compile(
  r'^(?P<month>[A-Z][a-z]{2})  (?P<day>[0-9]{2}) (?P<year>[0-9]{4}) '
  r'(?P<time>[0-9]{2}:[0-9]{2}:[0-9]{2})$')

_EMPTY_RE = re.compile(r'^(?P<empty>Log Empty END)$')
_COUNT_RE = re.compile(r'^(?P<nrresults>[0-9]{3})$')

# The reading can be HI (padded to three-characters by a space) if the value was
# over what the meter was supposed to read. Unlike the "Clock:" line, the months
# of June and July are written in full, everything else is truncated to three
# characters, so accept a space or 'e'/'y' at the end of the month name. Also,
# the time does *not* include seconds.
_READING_RE = re.compile(
  r'^(?P<reading>|[0-9]{3})  (?P<month>[A-Z][a-z]{2})  (?P<day>[0-9]{2}) '
  r'(?P<year>[0-9]{4}) (?P<time>[0-9]{2}:[0-9]{2}) '
  r'(?P<type>[0-9]{2}) (?P<sentinel>0x0[01])$')

_CHECKSUM_RE = re.compile(
  r'^(?P<checksum>0x[0-9A-F]{4})  END$')

# There are two date format used by the device. One uses three-letters month
# names, and that's easy enough. The other uses three-letters month names,
# except for (at least) July. So ignore the fourth character.
# explicit mapping. Note that the mapping *requires* a trailing whitespace.
_MONTH_MATCHES = {
  'Jan': 1,
  'Feb': 2,
  'Mar': 3,
  'Apr': 4,
  'May': 5,
  'Jun': 6,
  'Jul': 7,
  'Aug': 8,
  'Sep': 9,
  'Oct': 10,
  'Nov': 11,
  'Dec': 12
  }

_INFO_KEYS = [
  'device_version_',
  'software_revision_',
  'device_serialno_',
  'device_glucose_unit_',
  'device_current_date_time_',
  'device_nrresults_'
]

_READING_KEYS = [
  'reading',
  'month',
  'day',
  'year',
  'time',
  'type',
  'sentinel'
]

_INFO_SIZE = 5

def _parse_clock(datestr):
  """Convert the date/time string used by the the device into a datetime.

  Args:
    datestr: a string as returned by the device during information handling.
  """
  match = _CLOCK_RE.match(datestr)
  if not match:
    raise exceptions.InvalidResponse(datestr)
  # int() parses numbers in decimal, so we don't have to worry about '08'
  day = int(match.group('day'))
  month = _MONTH_MATCHES[match.group('month')]
  year = int(match.group('year'))
  time = match.group('time')
  hour, minute, second = map(int, time.split(':'))
  return datetime.datetime(year, month, day, hour, minute, second)

def _parse_nrresults(countstr):
  """Convert the count string used by the device into number of results.

  Args:
    countstr:  a string as returned by the device during information handling.
    Special case:  no results ('Log Empty END') returns 0.
  """
  match_empty = _EMPTY_RE.match(countstr)
  match_count = _COUNT_RE.match(countstr)
  if match_empty:
    return 0
  elif match_count:
    return int(countstr)
  else:
    raise exceptions.InvalidResponse(countstr)

def _parse_info(data):
  info = {}
  if len(data) < _INFO_SIZE:
    msg = '_parse_info:  len(data)=%d < %d lines:  ' % (len(data),_INFO_SIZE)
    logging.debug(msg + '%r', data)
    raise exceptions.InvalidResponse('\n'.join(data))
  if data[0] != '':
    msg = '_parse_info:  first line not blank:  '
    logging.debug(msg + '%r', data)
    raise exceptions.InvalidResponse('\n'.join(data))
  info['device_version_']           = data[1]
  info['software_revision_']        = data[2]
  info['device_serialno_']          = 'N/A'
  info['device_glucose_unit_']      = common.Unit.MG_DL
  # info['device_glucose_unit_']    = common.Unit.MMOL_L
  info['device_current_date_time_'] = _parse_clock(data[3])
  info['device_nrresults_']         = _parse_nrresults(data[4])
  return info

def _parse_resline(line, j):
  match = _READING_RE.match(line)
  if not match:
    raise exceptions.InvalidResponse('error line %d = %s' % (j, line))
  READING  = match.group('reading')
  MONTH    = match.group('month')
  DAY      = match.group('day')
  YEAR     = match.group('year')
  TIME     = match.group('time')
  TYPE     = match.group('type')
  SENTINEL = match.group('sentinel')
  if TYPE != '00':
    logging.warning('TYPE == %s  NORMALLY type == 00' % TYPE)
  if READING == 'HI ':
    value = float("inf")
  else:
    value = float(READING)
  month = _MONTH_MATCHES[MONTH]
  day = int(DAY)
  year = int(YEAR)
  hour, minute = map(int, TIME.split(':'))
  timestamp = datetime.datetime(year, month, day, hour, minute)
  # The reading, if present, is always in mg/dL even if the glucometer is
  # set to mmol/L.
  # fixme
  return { 'value'     : value,
           'day'       : day,
           'month'     : month,
           'year'      : year,
           'hour'      : hour,
           'minute'    : minute,
           'timestamp' : timestamp,
           'READING'   : READING,
           'TYPE'      : TYPE,
           'SENTINEL'  : SENTINEL
}

def _parse_checksum(line):
  match = _CHECKSUM_RE.match(line)
  if not match:
    raise exceptions.InvalidResponse('\n'.join(line))
  checksum_str = match.group('checksum')
  checksum = int(checksum_str, 16)
  return checksum

def _parse_result(data):
  """\
  I assume empty meter has final line "Log Empty END"
  and no trailing empty line:
  blank, dev, soft, date, "Log Empty END"
  I assume non-empty meter has lines:
  blank, dev, soft, date, count, blank line, result*count, checksum
  """
  info = _parse_info(data)
  n = info['device_nrresults_']
  if n == 0:
    len_data1 = _INFO_SIZE
  else:
    len_data1 = _INFO_SIZE + n + 2
    if data[_INFO_SIZE] != '':
      logging.debug('_parse_result:  last line not blank:  %r', data)
      raise exceptions.InvalidResponse('\n'.join(data))
  if len(data) != len_data1:
    msg = '_parse_result:  len(data)= %d correct len_data= %d  ' % (len(data), len_data1)
    logging.debug(msg + '%r', data)
    raise exceptions.InvalidResponse('\n'.join(data))
  j1 = _INFO_SIZE + 1
  j2 = _INFO_SIZE + 1 + n
  reslog = [_parse_resline(data[j],j) for j in range(j1, j2)]
  if n > 0:
    checksum = _parse_checksum(data[j2])
  return { 'info' : info, 'reslog' : reslog, 'checksum' : checksum }

class Device(serial.SerialDevice):
  BAUDRATE = 19200
  DEFAULT_CABLE_ID = '1a61:3410'

  def _send_command(self, command):
    cmd_bytes = bytes('$%s\r\n' % command, 'ascii')
    logging.debug('Sending command: %r', cmd_bytes)

    self.serial_.write(cmd_bytes)
    self.serial_.flush()

    response = self.serial_.readlines()

    logging.debug('Received response: %r', response)

    # We always want to decode the output, and remove stray \r\n. Any failure in
    # decoding means the output is invalid anyway.
    decoded_response = [line.decode('ascii').rstrip('\r\n')
                        for line in response]
    return decoded_response

  def connect(self):
    # self._send_command('x') # ignore output this time
    # self._send_command('mem') # ignore output this time
    self._fetch_device_information()

  def disconnect(self):
    return

  def _fetch_device_information(self):
    data = self._send_command('mem')
    res = _parse_result(data)
    self.info_                     = res['info']
    self.device_results_           = res['reslog']
    self.device_checksum_          = res['checksum']
    self.device_version_           = self.info_['device_version_']
    self.software_revision_        = self.info_['software_revision_']
    self.device_serialno_          = self.info_['device_serialno_']
    self.device_glucose_unit_      = self.info_['device_glucose_unit_']
    self.device_current_date_time_ = self.info_['device_current_date_time_']
    self.device_nrresults_         = self.info_['device_nrresults_']
    # exclude the last line which is the checksum itself.
    # every  line is ended with a windows end-of-line \r\n
    # except line 6  ends  with a linux   end-of-line \n
    n = self.device_nrresults_
    if n > 0:
      wnl = (ord('\r') + ord('\n')) * (_INFO_SIZE + n) # windows end of line
      lnl = ord('\n') # linux end of line
      ce = self.device_checksum_ # expected
      cg = (sum(ord(c) for c in ''.join(data[:-1])) + wnl + lnl) % (2 ** 16) # gotten
      if ce != cg:
        raise exceptions.InvalidChecksum(ce, cg)

  def get_meter_info(self):
    """Fetch and parses the device information.

    Returns:
      A common.MeterInfo object.
    """
    return common.MeterInfo(
      'Freestyle Lite glucometer',
      serial_number=self.get_serial_number(),
      version_info=('Software version: ' + self.get_software_revision(),
                    'Hardware version: ' + self.get_version()),
      native_unit=self.get_glucose_unit()
    )

  def get_version(self):
    """Returns an identifier of the firmware version of the glucometer.

    Returns:
      The hardware version returned by the glucometer, such as "0.22"
    """
    return self.device_version_

  def get_software_revision(self):
    """Returns an identifier of the software version of the glucometer.

    Returns:
      The software version returned by the glucometer, such as "0.22"
    """
    return self.software_revision_

  def get_serial_number(self):
    """Retrieve the serial number of the device.

    Returns:
      A string representing the serial number of the device.
    """
    return self.device_serialno_

  def get_glucose_unit(self):
    """Returns a constant representing the unit displayed by the meter.

    Returns:
      common.Unit.MG_DL: if the glucometer displays in mg/dL
      common.Unit.MMOL_L: if the glucometer displays in mmol/L
    """
    return self.device_glucose_unit_

  def get_datetime(self):
    """Returns the current date and time for the glucometer.

    Returns:
      A datetime object built according to the returned response.
    """
    return self.device_current_date_time_

  def set_datetime(self, date=datetime.datetime.now()):
    """Sets the date and time of the glucometer.

    Args:
      date: The value to set the date/time of the glucometer to. If none is
        given, the current date and time of the computer is used.

    Returns:
      A datetime object built according to the returned response.
    """
    raise NotImplementedError

  def zero_log(self):
    """Zeros out the data log of the device.

    This function will clear the memory of the device deleting all the readings
    in an irrecoverable way.
    """
    raise NotImplementedError

  def get_readings(self):
    """Iterates over the reading values stored in the glucometer.

    Args:
      unit: The glucose unit to use for the output.

    Yields:
      A tuple (date, value) of the readings in the glucometer. The value is a
      floating point in the unit specified; if no unit is specified, the default
      unit in the glucometer will be used.

    Raises:
      exceptions.InvalidResponse: if the response does not match what expected.
    """
    for line in self.device_results_:
        value     = line['value']
        day       = line['day']
        month     = line['month']
        year      = line['year']
        hour      = line['hour']
        minute    = line['minute']
        timestamp = line['timestamp']
        # fixme
        # The reading, if present, is always in mg/dL even if the glucometer is
        # set to mmol/L.
        yield common.GlucoseReading(timestamp, value)
