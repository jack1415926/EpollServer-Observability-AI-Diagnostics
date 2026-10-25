"""Short MySQL transactions on independent monitoring tables, never business tables."""
import json
import os
import time
import pymysql


def now_ms():
    return int(time.time()*1000)


class Store:
    def connect(self):
        if not os.getenv('EPOLL_MONITOR_DB_NAME'):
            raise RuntimeError('diagnosis_store_unconfigured')
        return pymysql.connect(host=os.getenv('EPOLL_MONITOR_DB_HOST', '127.0.0.1'),
            port=int(os.getenv('EPOLL_MONITOR_DB_PORT', '3306')),
            user=os.getenv('EPOLL_MONITOR_DB_USER', ''), password=os.getenv('EPOLL_MONITOR_DB_PASSWORD', ''),
            database=os.environ['EPOLL_MONITOR_DB_NAME'], charset='utf8mb4',
            connect_timeout=2, read_timeout=2, write_timeout=2,
            cursorclass=pymysql.cursors.DictCursor)

    def execute(self, operation):
        with self.connect() as connection:
            with connection.cursor() as cursor:
                value = operation(cursor)
            connection.commit()
            return value

    def initialize(self):
        def operation(cursor):
            cursor.execute("UPDATE diagnosis_task SET status='interrupted', error_code='service_restarted', updated_at_unix_ms=%s WHERE status IN ('queued','running')", (now_ms(),))
            # Read both tables now rather than accepting work with a missing report table.
            cursor.execute('SELECT id FROM diagnosis_report LIMIT 1')
        self.execute(operation)

    def create(self, key, question):
        stamp = now_ms()
        def operation(cursor):
            cursor.execute('DELETE FROM diagnosis_report WHERE expires_at_unix_ms <= %s LIMIT 100', (stamp,))
            cursor.execute('DELETE FROM diagnosis_task WHERE expires_at_unix_ms <= %s LIMIT 100', (stamp,))
            cursor.execute("INSERT INTO diagnosis_task (id,question,status,created_at_unix_ms,updated_at_unix_ms,expires_at_unix_ms) VALUES (%s,%s,'queued',%s,%s,%s)",
                           (key, question, stamp, stamp, stamp+7*86400000))
        self.execute(operation)

    def update(self, key, status, error=None, report=None):
        def operation(cursor):
            if report is not None:
                cursor.execute('INSERT INTO diagnosis_report (id,report,expires_at_unix_ms) SELECT id,%s,expires_at_unix_ms FROM diagnosis_task WHERE id=%s ON DUPLICATE KEY UPDATE report=VALUES(report)',
                               (json.dumps(report, ensure_ascii=False), key))
            cursor.execute('UPDATE diagnosis_task SET status=%s,error_code=%s,updated_at_unix_ms=%s WHERE id=%s', (status, error, now_ms(), key))
        self.execute(operation)

    def read(self, key=None):
        def operation(cursor):
            if key:
                cursor.execute('SELECT t.*,r.report FROM diagnosis_task t LEFT JOIN diagnosis_report r ON t.id=r.id WHERE t.id=%s AND t.expires_at_unix_ms>%s', (key, now_ms()))
                row = cursor.fetchone()
                if row and row['report'] is not None:
                    row['report'] = json.loads(row['report'])
                return row
            cursor.execute('SELECT id,question,status,error_code,created_at_unix_ms,updated_at_unix_ms,expires_at_unix_ms FROM diagnosis_task WHERE expires_at_unix_ms>%s ORDER BY created_at_unix_ms DESC,id DESC LIMIT 100', (now_ms(),))
            return cursor.fetchall()
        return self.execute(operation)
