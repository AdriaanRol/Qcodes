import math
import time
import visa
from datetime import datetime
from functools import partial

from qcodes.instrument.visa import VisaInstrument
from qcodes.utils import validators as vals


class QDac(VisaInstrument):
    '''
    Driver for the QDev digital-analog converter QDac
    Developed by QDev/NBI 2015-2016

    Based on "DAC commands 7.doc"
    Tested with Software Version: 0.160218
    '''

    # 2^28 - 1 - a few cmds go up to 2^31 - 1 but we should never need this.
    max_int = 268435455
    voltage_range_map = {10: 0, 1: 1}  # +/- 10V or +/- 1V
    voltage_range_status = {'X 1': 10, 'X 0.1': 1}

    current_range_map = {'pA': 0, 'nA': 1}

    # set nonzero value (seconds) to accept older status when reading settings
    max_status_age = 1

    def __init__(self, name, address, num_chans=48):
        super().__init__(name, address)
        handle = self.visa_handle

        # This is the baud rate on power-up. It can be changed later but
        # you must start out with this value.
        handle.baud_rate = 480600
        handle.parity = visa.constants.Parity(0)
        handle.data_bits = 8
        self.set_terminator('\n')
        # TODO: do we want a method for write termination too?
        handle.write_termination = '\n'
        # TODO: do we need a query delay for robust operation?

        self.num_chans = num_chans

        self.add_function('get_status', call_cmd=self._get_status)

        self.chan_range = range(1, 1 + self.num_chans)
        self.channel_validator = vals.Ints(1, self.num_chans)

        for i in self.chan_range:
            stri = str(i)
            self.add_parameter(name='v' + stri,
                               label='Channel ' + stri,
                               units='V',
                               set_cmd='set ' + stri + ' {:.6f}',
                               vals=vals.Numbers(-10, 10),
                               get_cmd=partial(self.read_state, i, 'v'))
            self.add_parameter(name='vrange' + stri,
                               set_cmd='vol ' + stri + ' {}',
                               val_mapping=self.voltage_range_map,
                               get_cmd=partial(self.read_state, i, 'vrange'))
            self.add_parameter(name='irange' + stri,
                               set_cmd='cur ' + stri + ' {}',
                               val_mapping=self.current_range_map,
                               get_cmd=partial(self.read_state, i, 'irange'))
            self.add_parameter(name='i' + stri,
                               label='Current ' + stri,
                               units='A',
                               get_cmd='get ' + stri,
                               get_parser=self._num_verbose)

        for board in range(6):
            for sensor in range(3):
                label = 'Board {}, Temperature {}'.format(board, sensor)
                self.add_parameter(name='temp{}_{}'.format(board, sensor),
                                   label=label,
                                   units='C',
                                   get_cmd='tem {} {}'.format(board, sensor),
                                   get_parser=self._num_verbose)

        self.add_parameter(name='cal',
                           set_cmd='cal {}',
                           vals=self.channel_validator)
        self.add_parameter(name='verbose',
                           set_cmd='ver {}',
                           val_mapping={True: 1, False: 0})

        waveform_params = [
            self.channel_validator,
            # 0 = DC, 1-8 = waveforms, 9 = AWG, 10 = pulse gen
            vals.Ints(0, 10),
            vals.Numbers(-10, 10),  # amplitude
            vals.Numbers(-10, 10)  # offset
        ]
        self.add_function(name='set_waveform',
                          call_cmd='wav {} {} {} {}',
                          parameters=waveform_params)

        function_params = [
            vals.Ints(1, 8),  # waveform slots
            vals.Ints(1, self.max_int),  # period, in milliseconds TODO: check
            vals.Numbers(0, 100)  # duty cycle (not for sin)
        ]
        self.add_function(name='create_sin',
                          call_cmd='fun {} 1 {}',
                          parameters=function_params[:2])
        self.add_function(name='create_square',
                          call_cmd='fun {} 2 {} {}',
                          parameters=function_params)
        self.add_function(name='create_triangle',
                          call_cmd='fun {} 3 {} {}',
                          parameters=function_params)

        self.add_function(name='create_raw_awg',
                          call_cmd=self._raw_awg,
                          parameters=[vals.Anything()])
        self.add_function(name='create_linear_awg',
                          call_cmd=self._linear_awg,
                          parameters=[vals.Anything()])
        self.add_function(name='create_spline_awg',
                          call_cmd=self._spline_awg,
                          parameters=[vals.Numbers(1, self.max_int),
                                      vals.Anything()])

        pulse_params = [
            # low and high times in milliseconds
            vals.Ints(1, self.max_int),
            vals.Ints(1, self.max_int),
            # low and high values in volts
            vals.Numbers(-10, 10),
            vals.Numbers(-10, 10),
            # pulse count (0 is forever)
            vals.Ints(0, self.max_int)
        ]
        self.add_function(name='create_pulses',
                          call_cmd='pul {} {} {} {} {}',
                          parameters=pulse_params)

        sync_params = [
            # sync outputs - command supports 6 but 6th is given up
            # for the calibration port
            vals.Ints(1, 5),
            # which function generator (1-8 are funcs, 9 is awg, 10 is pulse)
            vals.Ints(1, 10),
            vals.Ints(0, self.max_int),  # msec delay vs start of waveform
            vals.Ints(1, self.max_int),  # pulse length, in milliseconds
            # repetitions is currently broken - TODO reinstate when Rikke fixes
            # vals.Ints(0, 1000000000)  # repetitions (0 means forever)
        ]
        self.add_function(name='set_sync_pulse',
                          call_cmd='syn {} {} {} {}',
                          parameters=sync_params)

        self.add_function(name='soft_sync',
                          call_cmd=self._soft_sync,
                          parameters=[vals.Ints(1, 10)])

        # not to be implemented:
        # boa, tri (service), val, upd, sin (obsolete)

        # not implemented yet:
        # nice interface to waveforms

        self.verbose.set(False)
        self.get_status()
        print('connected to QDac on {}, firmware version {}'.format(
            self._address, self.version))

    def _num_verbose(self, s):
        '''
        turn a return value from the QDac into a number.
        If the QDac is in verbose mode, this involves stripping off the
        value descriptor.
        '''
        if self.verbose.get_latest():
            s = s.split[': '][-1]
        return float(s)

    def read_state(self, chan, param):
        '''
        specific routine for reading items out of status response
        '''
        if chan not in self.chan_range:
            raise ValueError('valid channels are {}'.format(self.chan_range))
        valid_params = ('v', 'vrange', 'irange')
        if param not in valid_params:
            raise ValueError(
                'read_state valid params are {}'.format(valid_params))

        if not (self.max_status_age and (
                    datetime.now() - self._status_ts
                ).total_seconds() < self.max_status_age):
            self.get_status()

        return self.parameters[param + str(chan)].get_latest()

    def _get_status(self):
        r'''
        status call generates 51 lines of output. Send the command and
        read the first one, which is the software version line

        the full output looks like:
        Software Version: 0.160218\r\n
        Channel\tOut V\t\tVoltage range\tCurrent range\n
        \n
        8\t  0.000000\t\tX 1\t\tpA\n
        7\t  0.000000\t\tX 1\t\tpA\n
        ... (all 48 channels like this in a somewhat peculiar order)
        (no termination afterward besides the \n ending the last channel)

        returns a list of dicts [{v, vrange, irange}]
        NOTE - channels are 1-based, but the return is a list, so of course
        0-based, ie chan1 is out[0]
        '''
        version_line = self.ask('status')
        if version_line.startswith('Software Version: '):
            self.version = version_line.strip().split(': ')[1]
        else:
            self._wait_and_clear()
            raise ValueError('unrecognized version line: ' + version_line)

        header_line = self.read()
        headers = header_line.lower().strip('\r\n').split('\t')
        expected_headers = ['channel', 'out v', '', 'voltage range',
                            'current range']
        if headers != expected_headers:
            raise ValueError('unrecognized header line: ' + header_line)

        chans = [{} for i in self.chan_range]
        chans_left = set(self.chan_range)
        while chans_left:
            line = self.read().strip()
            if not line:
                continue
            chanstr, v, _, vrange, _, irange = line.split('\t')
            chan = int(chanstr)

            vals_dict = {
                'v': float(v),
                'vrange': self.voltage_range_status[vrange.strip()],
                'irange': irange
            }

            chans[chan - 1] = vals_dict
            for param, val in vals_dict.items():
                self.parameters[param + chanstr]._save_val(val)

            chans_left.remove(chan)

        self._status = chans
        self._status_ts = datetime.now()
        return chans

    def _write_awg(self, type, interval, data, chunklen=64):
        '''
        low-level awg command, assumes type is already encoded (0, 1, 2)
        and data is already a flat list of strings
        stores the shape in waveform 9
        '''
        chunks = math.ceil(len(data) / chunklen)
        for i in range(len(chunks)):
            start, stop = i * chunklen, (i + 1) * chunklen
            datastr = ' '.join([str(d) for d in data[start: stop]])
            self.write('awg {} {} {}'.format(type, interval, datastr))
        self.write('run')

    def _raw_awg(self, data):
        self._write_awg(0, 0, list(map(str, data)))

    def _linear_awg(self, data):
        self._write_awg(1, 0, ['{} {}'.format(*pt) for pt in data])

    def _spline_awg(self, interval, data):
        self._write_awg(2, interval, data)

    def _soft_sync(self, func_gen):
        self.ask('ssy {}'.format(func_gen))

        # wait for the next response, which should be the soft sync
        # we don't DO anything after this, just return.
        resp = self.read()
        if resp != '#{:02d}'.format(func_gen):
            raise RuntimeError('expected soft sync response, got: ' + resp)

    def write(self, cmd):
        '''
        QDac always returns something even from set commands, even when
        verbose mode is off, so we'll override write to take this out
        if you want to use this response, we put it in self._write_response
        (but only for the very last write call)
        '''
        nr_bytes_written, ret_code = self.visa_handle.write(cmd)
        self.check_error(ret_code)
        self._write_response = self.visa_handle.read()

    def read(self):
        # TODO: make this a base class method?
        return self.visa_handle.read()

    def _wait_and_clear(self, delay=0.5):
        time.sleep(delay)
        self.visa_handle.clear()
