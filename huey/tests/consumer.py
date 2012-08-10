import datetime
import logging
import threading
import time
import unittest

from huey import queue_command, Invoker, BaseConfiguration
from huey.backends.dummy import DummyQueue, DummyDataStore
from huey.exceptions import QueueException
from huey.queue import QueueCommand, PeriodicQueueCommand
from huey.registry import registry
from huey.utils import local_to_utc
from huey.bin.huey_consumer import load_config, Consumer, IterableQueue


# store some global state
state = {}

# create a queue, result store and invoker for testing
test_queue = DummyQueue('test-queue')
test_result_store = DummyDataStore('test-queue')
test_task_store = DummyDataStore('test-tasks')
test_invoker = Invoker(test_queue, test_result_store, test_task_store)

# create a dummy config for passing to the consumer
class DummyConfiguration(BaseConfiguration):
    QUEUE = test_queue
    RESULT_STORE = test_result_store
    TASK_STORE = test_result_store
    THREADS = 2

@queue_command(test_invoker)
def modify_state(k, v):
    state[k] = v
    return v

@queue_command(test_invoker)
def blow_up():
    raise Exception('blowed up')

@queue_command(test_invoker, retries=3)
def retry_command(k, always_fail=True):
    if k not in state:
        if not always_fail:
            state[k] = 'fixed'
        raise Exception('fappsk')
    return state[k]

@queue_command(test_invoker, retries=3, retry_delay=10)
def retry_command_slow(k, always_fail=True):
    if k not in state:
        if not always_fail:
            state[k] = 'fixed'
        raise Exception('fappsk')
    return state[k]

# create a log handler that will track messages generated by the consumer
class TestLogHandler(logging.Handler):
    def __init__(self, *args, **kwargs):
        self.messages = []
        logging.Handler.__init__(self, *args, **kwargs)

    def emit(self, record):
        self.messages.append(record.getMessage())


class SkewConsumerTestCase(unittest.TestCase):
    def setUp(self):
        global state
        state = {}

        self.orig_sleep = time.sleep
        time.sleep = lambda x: None

        self.consumer = Consumer(test_invoker, DummyConfiguration)
        self.handler = TestLogHandler()
        self.consumer.logger.addHandler(self.handler)

    def tearDown(self):
        self.consumer.shutdown()
        self.consumer.logger.removeHandler(self.handler)
        time.sleep = self.orig_sleep

    def test_consumer_loader(self):
        config = load_config('huey.tests.config.Config')
        self.assertTrue(isinstance(config.QUEUE, DummyQueue))
        self.assertEqual(config.QUEUE.name, 'test-queue')

    def spawn(self, func, *args, **kwargs):
        t = threading.Thread(target=func, args=args, kwargs=kwargs)
        t.start()
        return t

    def test_iterable_queue(self):
        store = []
        q = IterableQueue()

        def do_queue(queue, result):
            for message in queue:
                result.append(message)

        t = self.spawn(do_queue, q, store)
        q.put(1)
        q.put(2)
        q.put(StopIteration)

        t.join()
        self.assertFalse(t.is_alive())
        self.assertEqual(store, [1, 2])

    def test_message_processing(self):
        self.consumer.start_message_receiver()
        self.consumer.start_worker_pool()

        self.assertFalse('k' in state)

        res = modify_state('k', 'v')
        res.get(blocking=True)

        self.assertTrue('k' in state)
        self.assertEqual(res.get(), 'v')

    def test_worker(self):
        res = modify_state('x', 'y')

        cmd = test_invoker.dequeue()
        self.assertEqual(res.get(), None)

        # we will be calling release() after finishing work
        self.consumer._pool.acquire()
        self.consumer.worker(cmd)

        self.assertTrue('x' in state)
        self.assertEqual(res.get(), 'y')

    def test_worker_exception(self):
        res = blow_up()
        cmd = test_invoker.dequeue()

        self.consumer._pool.acquire()
        self.consumer.worker(cmd)

        self.assertEqual(self.handler.messages, [
            'unhandled exception in worker thread',
        ])

    def test_retries_and_logging(self):
        # this will continually fail
        res = retry_command('blampf')

        cmd = test_invoker.dequeue()
        self.consumer._pool.acquire()
        self.consumer.worker(cmd)
        self.assertEqual(self.handler.messages, [
            'unhandled exception in worker thread',
            're-enqueueing task %s, 2 tries left' % cmd.task_id,
        ])

        cmd = test_invoker.dequeue()
        self.assertEqual(cmd.retries, 2)
        self.consumer._pool.acquire()
        self.consumer.worker(cmd)
        self.assertEqual(self.handler.messages[-2:], [
            'unhandled exception in worker thread',
            're-enqueueing task %s, 1 tries left' % cmd.task_id,
        ])

        cmd = test_invoker.dequeue()
        self.assertEqual(cmd.retries, 1)
        self.consumer._pool.acquire()
        self.consumer.worker(cmd)
        self.assertEqual(self.handler.messages[-2:], [
            'unhandled exception in worker thread',
            're-enqueueing task %s, 0 tries left' % cmd.task_id,
        ])

        cmd = test_invoker.dequeue()
        self.assertEqual(cmd.retries, 0)
        self.consumer._pool.acquire()
        self.consumer.worker(cmd)
        self.assertEqual(len(self.handler.messages), 7)
        self.assertEqual(self.handler.messages[-1:], [
            'unhandled exception in worker thread',
        ])

        self.assertEqual(test_invoker.dequeue(), None)

    def test_retries_with_success(self):
        # this will fail once, then succeed
        res = retry_command('blampf', False)
        self.assertFalse('blampf' in state)

        cmd = test_invoker.dequeue()
        self.consumer._pool.acquire()
        self.consumer.worker(cmd)
        self.assertEqual(self.handler.messages, [
            'unhandled exception in worker thread',
            're-enqueueing task %s, 2 tries left' % cmd.task_id,
        ])

        cmd = test_invoker.dequeue()
        self.assertEqual(cmd.retries, 2)
        self.consumer._pool.acquire()
        self.consumer.worker(cmd)

        self.assertEqual(state['blampf'], 'fixed')

        self.assertEqual(test_invoker.dequeue(), None)

    def test_pooling(self):
        # simulate acquiring two worker threads
        self.consumer._pool.acquire()
        self.consumer._pool.acquire()

        res = modify_state('x', 'y')

        # dequeue a *single* message
        pt = self.spawn(self.consumer.check_message)

        # work on any messages generated by the processor thread
        st = self.spawn(self.consumer.worker_pool)

        # our result is not available since all workers are blocked
        self.assertEqual(res.get(), None)
        self.assertFalse(self.consumer._pool.acquire(blocking=False))

        # our processor is waiting
        self.assertTrue(pt.is_alive())
        self.assertEqual(self.consumer._queue.qsize(), 0)

        # release a worker
        self.consumer._pool.release()

        # we can get and block now, but will set a timeout of 3 to indicate that
        # something is wrong
        self.assertEqual(res.get(blocking=True, timeout=3), 'y')

        # this is done
        pt.join()

    def test_scheduling(self):
        dt = datetime.datetime(2011, 1, 1, 0, 0)
        dt2 = datetime.datetime(2037, 1, 1, 0, 0)
        r1 = modify_state.schedule(args=('k', 'v'), eta=dt, convert_utc=False)
        r2 = modify_state.schedule(args=('k2', 'v2'), eta=dt2, convert_utc=False)

        # dequeue a *single* message
        pt = self.spawn(self.consumer.check_message)

        # work on any messages generated by the processor thread
        st = self.spawn(self.consumer.worker_pool)

        pt.join()
        self.assertTrue('k' in state)
        self.assertEqual(self.consumer.schedule._schedule, {})

        # dequeue a *single* message
        pt = self.spawn(self.consumer.check_message)
        pt.join()

        # it got stored in the schedule instead of executing
        self.assertFalse('k2' in state)
        self.assertTrue(r2.task_id in self.consumer.schedule._schedule)

        # run through an iteration of the scheduler
        self.consumer.check_schedule(dt)

        # our command was not enqueued
        self.assertEqual(len(self.consumer.invoker.queue), 0)

        # try running the scheduler with the time the command should run
        self.consumer.check_schedule(dt2)

        # it was enqueued
        self.assertEqual(len(self.consumer.invoker.queue), 1)
        self.assertEqual(self.consumer.schedule._schedule, {})

        # dequeue and inspect -- it won't be executed because the scheduler will
        # see that it is scheduled to run in the future and plop it back into the
        # schedule
        command = self.consumer.invoker.dequeue()
        self.assertEqual(command.task_id, r2.task_id)
        self.assertEqual(command.execute_time, dt2)

    def test_retry_scheduling(self):
        # this will continually fail
        res = retry_command_slow('blampf')
        self.assertEqual(self.consumer.schedule._schedule, {})

        cur_time = datetime.datetime.utcnow()

        cmd = test_invoker.dequeue()
        self.consumer._pool.acquire()
        self.consumer.worker(cmd)
        self.assertEqual(self.handler.messages, [
            'unhandled exception in worker thread',
            're-enqueueing task %s, 2 tries left' % cmd.task_id,
        ])

        self.assertEqual(self.consumer.schedule._schedule, {
            cmd.task_id: cmd,
        })
        cmd_from_sched = self.consumer.schedule._schedule[cmd.task_id]
        self.assertEqual(cmd_from_sched.retries, 2)
        exec_time = cmd.execute_time

        self.assertEqual((exec_time - cur_time).seconds, 10)

    def test_schedule_local_utc(self):
        dt = datetime.datetime(2011, 1, 1, 0, 0)
        dt2 = datetime.datetime(2037, 1, 1, 0, 0)
        r1 = modify_state.schedule(args=('k', 'v'), eta=dt)
        r2 = modify_state.schedule(args=('k2', 'v2'), eta=dt2)

        # dequeue a *single* message
        pt = self.spawn(self.consumer.check_message)

        # work on any messages generated by the processor thread
        st = self.spawn(self.consumer.worker_pool)

        pt.join()
        self.assertTrue('k' in state)
        self.assertEqual(self.consumer.schedule._schedule, {})

        # dequeue a *single* message
        pt = self.spawn(self.consumer.check_message)
        pt.join()

        # it got stored in the schedule instead of executing
        self.assertFalse('k2' in state)
        self.assertTrue(r2.task_id in self.consumer.schedule._schedule)

        # run through an iteration of the scheduler
        self.consumer.check_schedule(dt)

        # our command was not enqueued
        self.assertEqual(len(self.consumer.invoker.queue), 0)

        # try running the scheduler with the time the command should run
        self.consumer.check_schedule(local_to_utc(dt2))

        # it was enqueued
        self.assertEqual(len(self.consumer.invoker.queue), 1)
        self.assertEqual(self.consumer.schedule._schedule, {})

        # dequeue and inspect -- it won't be executed because the scheduler will
        # see that it is scheduled to run in the future and plop it back into the
        # schedule
        command = self.consumer.invoker.dequeue()
        self.assertEqual(command.task_id, r2.task_id)
        self.assertEqual(command.execute_time, local_to_utc(dt2))

    def test_schedule_persistence(self):
        dt = datetime.datetime(2037, 1, 1, 0, 0)
        dt2 = datetime.datetime(2037, 1, 1, 0, 1)
        r = modify_state.schedule(args=('k', 'v'), eta=dt, convert_utc=False)
        r2 = modify_state.schedule(args=('k2', 'v2'), eta=dt2, convert_utc=False)

        # two messages in the queue
        self.assertEqual(len(self.consumer.invoker.queue), 2)

        # pull 'em down
        self.consumer.check_message()
        self.consumer.check_message()

        self.consumer.save_schedule()
        self.consumer.schedule._schedule = {}

        self.consumer.load_schedule()
        self.assertTrue(r.task_id in self.consumer.schedule._schedule)
        self.assertTrue(r2.task_id in self.consumer.schedule._schedule)

        cmd1 = self.consumer.schedule._schedule[r.task_id]
        cmd2 = self.consumer.schedule._schedule[r2.task_id]

        self.assertEqual(cmd1.execute_time, dt)
        self.assertEqual(cmd2.execute_time, dt2)

        # check w/conversion
        r3 = modify_state.schedule(args=('k3', 'v3'), eta=dt)
        self.consumer.check_message()

        self.consumer.save_schedule()
        self.consumer.schedule._schedule = {}

        self.consumer.load_schedule()
        cmd3 = self.consumer.schedule._schedule[r3.task_id]
        self.assertEqual(cmd3.execute_time, local_to_utc(dt))
