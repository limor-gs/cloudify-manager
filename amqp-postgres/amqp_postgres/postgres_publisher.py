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
from time import time
from threading import Thread, Lock

import psycopg2
from psycopg2.extras import execute_batch


EVENT_INSERT_QUERY = """
    INSERT INTO events (
        timestamp,
        reported_timestamp,
        _execution_fk,
        _tenant_id,
        _creator_id,
        event_type,
        message,
        message_code,
        operation,
        node_id,
        error_causes)
    SELECT
        now() AT TIME ZONE 'utc',
        CAST (%s AS TIMESTAMP),
        _storage_id,
        _tenant_id,
        _creator_id,
        %s,
        %s,
        %s,
        NULLIF(%s, ''),
        NULLIF(%s, ''),
        NULLIF(%s, '')
    FROM executions WHERE id = %s
"""

LOG_INSERT_QUERY = """
    INSERT INTO logs (
        timestamp,
        reported_timestamp,
        _execution_fk,
        _tenant_id,
        _creator_id,
        logger,
        level,
        message,
        message_code,
        operation,
        node_id)
    SELECT
        now() AT TIME ZONE 'utc',
        CAST (%s AS TIMESTAMP),
        _storage_id,
        _tenant_id,
        _creator_id,
        %s,
        %s,
        %s,
        %s,
        NULLIF(%s, ''),
        NULLIF(%s, '')
    FROM executions
    WHERE id = %s
"""


class DBLogEventPublisher(object):
    COMMIT_DELAY = 0.1  # seconds

    def __init__(self, config):
        self._lock = Lock()
        self._batch = Queue.Queue()

        self._last_commit = time()
        self.config = config

        # Create a separate thread to allow proper batching without losing
        # messages. Without this thread, if the messages were just committed,
        # and 1 new message is sent, then process will never commit, because
        # batch size wasn't exceeded and commit delay hasn't passed yet
        publish_thread = Thread(target=self._message_publisher)
        publish_thread.daemon = True
        publish_thread.start()

    def process(self, message, exchange):
        item = self._get_item(message, exchange)
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
                    execute_batch(cur, EVENT_INSERT_QUERY, events)
                conn.commit()
                events = []
                self._last_commit = time()
            if len(logs) > 100 or \
                    (logs and (time() - self._last_commit > 0.5)):
                with conn.cursor() as cur:
                    execute_batch(cur, LOG_INSERT_QUERY, logs)
                conn.commit()
                logs = []
                self._last_commit = time()

    def _get_item(self, message, exchange):
        if exchange == 'cloudify-events':
            return 'event', self._get_event(message)
        elif exchange == 'cloudify-logs':
            return 'log', self._get_log(message)
        else:
            raise ValueError('Unknown exchange type: {0}'.format(exchange))

    @staticmethod
    def _get_log(message):
        return (
            message['timestamp'],
            message['logger'],
            message['level'],
            message['message']['text'],
            message['message_code'],
            message['context'].get('operation'),
            message['context'].get('node_id'),
            message['context'].get('execution_id')
        )

    @staticmethod
    def _get_event(message):
        return (
            message['timestamp'],
            message['event_type'],
            message['message']['text'],
            message['message_code'],
            message['context'].get('operation'),
            message['context'].get('node_id'),
            message['context'].get('task_error_causes'),
            message['context'].get('execution_id')
        )
