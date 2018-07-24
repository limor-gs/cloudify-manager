########
# Copyright (c) 2018 Cloudify Platform Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
############

import Queue
import psycopg2
from uuid import uuid4
from time import time
from threading import Thread, Lock
from collections import OrderedDict, namedtuple

from manager_rest.storage.models import Event, Log, Execution

Exec = namedtuple('Execution', 'storage_id creator_id tenant_id')


LOG_INSERT_QUERY = Log.__table__.insert()
EVENT_INSERT_QUERY = Event.__table__.insert()


class DBLogEventPublisher(object):
    COMMIT_DELAY = 0.1  # seconds

    def __init__(self, config):
        self._lock = Lock()
        self._batch = Queue.Queue()

        self._last_commit = time()
        self.config = config
        self._executions_cache = LimitedSizeDict(10000)

        # Create a separate thread to allow proper batching without losing
        # messages. Without this thread, if the messages were just committed,
        # and 1 new message is sent, then process will never commit, because
        # batch size wasn't exceeded and commit delay hasn't passed yet
        publish_thread = Thread(target=self._message_publisher)
        publish_thread.daemon = True
        publish_thread.start()

    def process(self, message, exchange):
        execution = self._get_current_execution(message)
        item = self._get_item(message, exchange, execution)
        self._batch.put(item)

    def _message_publisher(self):
        conn = psycopg2.connect(
            dbname=self.config['postgresql_db_name'],
            host=self.config['postgresql_host'],
            user=self.config['postgresql_username'],
            password=self.config['postgresql_password']
        )
        events, logs = [], []
        while True:
            try:
                kind, item = self._batch.get(0.3)
            except Queue.Empty:
                pass
            else:
                target = events if kind == 'event' else logs
                target.append(item)
            if len(events) > 100 or \
                    (events and (time() - self._last_commit > 0.5)):
                with conn.cursor() as cur:
                    cur.execute_batch(EVENT_INSERT_QUERY, events)
                conn.commit()
                events = []
                self._last_commit = time()
            if len(logs) > 100 or \
                    (logs and (time() - self._last_commit > 0.5)):
                with conn.cursor() as cur:
                    cur.execute_batch(LOG_INSERT_QUERY, logs)
                conn.commit()
                logs = []
                self._last_commit = time()

    def _get_current_execution(self, message):
        """ Return execution from cache if exists, or from DB if needed """

        execution_id = message['context']['execution_id']
        execution = self._executions_cache.get(execution_id)
        if not execution:
            db_execution = Execution.query.filter_by(id=execution_id).first()
            execution = Exec(
                storage_id=db_execution._storage_id,
                creator_id=db_execution._creator_id,
                tenant_id=db_execution._tenant_id
            )
            self._executions_cache[execution_id] = execution
        return execution

    def _get_item(self, message, exchange, execution):
        if exchange == 'cloudify-events':
            return 'event', self._get_event(message, execution)
        elif exchange == 'cloudify-logs':
            return 'log', self._get_log(message, execution)
        else:
            raise ValueError('Unknown exchange type: {0}'.format(exchange))

    @staticmethod
    def _get_log(message, execution):
        return dict(
            id=str(uuid4()),
            reported_timestamp=message['timestamp'],
            logger=message['logger'],
            level=message['level'],
            message=message['message']['text'],
            operation=message['context'].get('operation'),
            node_id=message['context'].get('node_id'),
            _execution_fk=execution.storage_id,
            _tenant_id=execution.tenant_id,
            _creator_id=execution.creator_id
        )

    @staticmethod
    def _get_event(message, execution):
        return dict(
            id=str(uuid4()),
            reported_timestamp=message['timestamp'],
            event_type=message['event_type'],
            message=message['message']['text'],
            operation=message['context'].get('operation'),
            node_id=message['context'].get('node_id'),
            error_causes=message['context'].get('task_error_causes'),
            _execution_fk=execution.storage_id,
            _tenant_id=execution.tenant_id,
            _creator_id=execution.creator_id
        )


class LimitedSizeDict(OrderedDict):
    """
    A FIFO dictionary with a maximum size limit. If number of keys reaches
    the limit, the elements added first will be popped
    Implementation taken from https://stackoverflow.com/a/2437645/978089
    """
    def __init__(self, size_limit=None, *args, **kwds):
        self.size_limit = size_limit
        OrderedDict.__init__(self, *args, **kwds)
        self._check_size_limit()

    def __setitem__(self, key, value):
        OrderedDict.__setitem__(self, key, value)
        self._check_size_limit()

    def _check_size_limit(self):
        if self.size_limit is not None:
            while len(self) > self.size_limit:
                self.popitem(last=False)
