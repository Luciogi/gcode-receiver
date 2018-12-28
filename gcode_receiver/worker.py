import logging
from multiprocessing.queues import Empty as MultiprocessingQueueEmpty
import time

from six.moves.queue import Empty, Queue

from .commands import GcodeCommand, GrblRealtimeCommand
from .responses import StatusResponse


logger = logging.getLogger(__name__)


class WorkerException(Exception):
    pass


class Incomplete(WorkerException):
    # Used for delaying marking a command as completed
    pass


class EmptyCommandQueue(WorkerException):
    # Used for indicating that the command queue is empty
    pass


class Worker(object):
    def __init__(self, inqueue, outqueue, move_delay=0, **kwargs):
        # These queues are for communication only
        self._inqueue = inqueue
        self._outqueue = outqueue
        self._move_delay = move_delay

        self.initialize()

    def initialize(self):
        self._commands = Queue()
        self._command = None
        self._command_meta = {}

        self._absolute = True
        self._metric = True

        self._feed_rate = 0
        self._spindle_on = False
        self._spindle_speed = 0
        self._spindle_clockwise = True

        self._x = 0
        self._y = 0
        self._z = 0

    @classmethod
    def create(cls, **kwargs):
        self = Worker(**kwargs)
        self.start()

    def next_command(self):
        if self._commands.empty():
            return False

        try:
            self._command = self._commands.get_nowait()
            logger.debug(
                'Worker beginning processing of command: %s',
                self._command
            )
            return True
        except Empty:
            self._command = None
            return False

    def command_finished(self):
        logger.debug(
            'Worker finished processing of command: %s',
            self._command
        )
        self._command = None
        self._command_meta = {}

    @property
    def command_meta(self):
        return self._command_meta

    @command_meta.setter
    def command_meta(self, value):
        self._command_meta = value

    @property
    def command(self):
        if not self._command:
            self.next_command()

        return self._command

    def handle_gcode_F(self):
        """Set feed rate"""
        self._feed_rate = self.command.as_dict()['F']

    def handle_gcode_G0(self):
        """Rapid move"""
        if 'delay_until' not in self.command_meta:
            self.command_meta['delay_until'] = time.time() + self._move_delay
        if time.time() < self.command_meta['delay_until']:
            raise Incomplete()

        data = self.command.as_dict()
        self._x = data.get('X', self._x)
        self._y = data.get('Y', self._y)
        self._z = data.get('Z', self._z)

    def handle_gcode_G1(self):
        """Linear move"""
        if 'delay_until' not in self.command_meta:
            self.command_meta['delay_until'] = time.time() + self._move_delay
        if time.time() < self.command_meta['delay_until']:
            raise Incomplete()

        data = self.command.as_dict()
        self._x = data.get('X', self._x)
        self._y = data.get('Y', self._y)
        self._z = data.get('Z', self._z)

    def handle_gcode_G4(self):
        """Dwell"""
        pass

    def handle_gcode_G20(self):
        """Set units to inches"""
        self._metric = False

    def handle_gcode_G21(self):
        """Set units to millimeters"""
        self._metric = True

    def handle_gcode_G90(self):
        """Absolute positioning"""
        self._absolute = True

    def handle_gcode_G91(self):
        """Relative positioning"""
        self._absolute = False

    def handle_gcode_G94(self):
        """Feed rate mode: Units/min"""
        pass

    def handle_gcode_M2(self):
        """Program End"""
        self._spindle_on = False

    def handle_gcode_M3(self):
        """Spindle ON (CW)"""
        self._spindle_on = True
        self._spindle_clockwise = True

        data = self.command.as_dict()
        self._spindle_speed = data.get('S', self._spindle_speed)

    def handle_gcode_M4(self):
        """Spindle ON (CCW)"""
        self._spindle_on = True
        self._spindle_clockwise = False

        data = self.command.as_dict()
        self._spindle_speed = data.get('S', self._spindle_speed)

    def handle_gcode_M5(self):
        """Spindle OFF"""
        self._spindle_on = False

    def tick(self):
        if not self.command:
            time.sleep(0.1)
            raise EmptyCommandQueue()

        handler_name_options = [
            'handle_gcode_{name}'.format(
                name=self.command.get_name()
            ),
            'handle_gcode_{field}'.format(
                field=self.command.get_main_field()
            )
        ]
        for handler_name in handler_name_options:
            if hasattr(self, handler_name):
                return getattr(self, handler_name)()

        logger.debug(
            "Gcode command not implemented: %s",
            str(self.command.get_name())
        )

    def handle_realtime(self, cmd):
        if cmd.char == GrblRealtimeCommand.STATUS:
            return StatusResponse(
                state=(
                    StatusResponse.RUN if self.command else StatusResponse.IDLE
                ),
                x=self._x,
                y=self._y,
                z=self._z,
                feed_rate=self._feed_rate,
                spindle_speed=self._spindle_speed
            )
        elif cmd.char == GrblRealtimeCommand.SOFT_RESET:
            self.initialize()
        else:
            logger.debug(
                "Realtime command not implemented: %s",
                str(cmd)
            )

    def enqueue_gcode(self, cmd):
        self._commands.put(cmd)

    def emit_response(self, response):
        logger.debug(
            'Enqueueing worker response: %s',
            response,
        )
        self._outqueue.put(response)

    def start(self):
        while True:
            try:
                response = self.tick()
                if response is not None:
                    self.emit_response(response)
            except (Incomplete, EmptyCommandQueue):
                pass
            else:
                self.command_finished()

            while not self._inqueue.empty():
                try:
                    cmd = self._inqueue.get_nowait()
                except MultiprocessingQueueEmpty:
                    continue

                logger.debug('Worker received command: %s', cmd)

                response = None
                if isinstance(cmd, GrblRealtimeCommand):
                    response = self.handle_realtime(cmd)
                    if response is not None:
                        self.emit_response(response)
                elif isinstance(cmd, GcodeCommand):
                    self.enqueue_gcode(cmd)
