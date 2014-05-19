import time
import logging

from model.base import init_db, db_session
from model.user import User
from model.queue import Queue
from management import PulseManagementAPI
from sendemail import sendemail
import config

logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class PulseGuardian(object):

    def __init__(self, api, emails=True, warn_queue_size=config.warn_queue_size,
                 del_queue_size=config.del_queue_size):
        self.api = api

        self.emails = emails
        self.warn_queue_size = warn_queue_size
        self.del_queue_size = del_queue_size

        self.warned_queues = set()
        self.deleted_queues = set()

    def delete_zombie_queues(self, queues):
        db_queues = Queue.query.all()

        # Filter queues that are in the database but no longer on RabbitMQ.
        alive_queues_names = {q['name'] for q in queues}
        zombie_queues = {q for q in db_queues if not q.name
                         in alive_queues_names}

        # Delete those queues.
        for queue in zombie_queues:
            db_session.delete(queue)
        db_session.commit()

    def monitor_queues(self, queues):
        for queue_data in queues:
            q_size, q_name, q_vhost = (queue_data['messages'],
                                       queue_data['name'], queue_data['vhost'])
            queue = Queue.query.filter(Queue.name == q_name).first()

            # If the queue doesn't exist in the db, create it.
            if queue is None:
                logger.info(". New queue '{}' encountered. "
                            "Adding to the database.".format(q_name))
                queue = Queue(name=q_name, owner=None)
                db_session.add(queue)
                db_session.commit()

            # Update the saved queue size.
            queue.size = q_size

            # If a queue is over the deletion size, regardless of it having an
            # owner or not, delete it.
            if q_size > self.del_queue_size:
                logger.warning("Queue '{}' deleted. Queue size = {}; del_queue_size = {}".format(
                    q_name, q_size, self.del_queue_size))
                if queue.owner:
                    self.deletion_email(queue.owner, queue_data)
                self.deleted_queues.add(queue.name)
                self.api.delete_queue(vhost=q_vhost, queue=q_name)
                db_session.delete(queue)
                db_session.commit()
                continue

            # If we don't know who created the queue...
            if queue.owner is None:
                # logger.info(". Queue '{}' owner's unknown.".format(q_name))

                # If no client is currently consuming the queue, just skip it.
                if queue_data['consumers'] == 0:
                    # logger.info(". Queue '{}' skipped (no owner, no current consumer).".format(q_name))
                    continue

                # Otherwise look for its user.
                owner_name = self.api.queue_owner(queue_data)

                user = User.query.filter(User.username == owner_name).first()

                # If the queue was created by a user that isn't in the
                # pulseguardian database, skip the queue.
                if user is None:
                    logger.info(
                        ". Queue '{}' owner, {}, isn't in the db. Skipping the queue.".format(q_name, owner_name))
                    continue

                # Assign the user to the queue.
                logger.info(
                    ". Assigning queue '{}'  to user {}.".format(q_name, user))
                queue.owner = user

            if q_size > self.warn_queue_size and not queue.warned:
                logger.warning("Warning queue '{}' owner. Queue size = {}; warn_queue_size = {}".format(
                    q_name, q_size, self.warn_queue_size))
                queue.warned = True
                self.warned_queues.add(queue.name)
                self.warning_email(queue.owner, queue_data)
            elif q_size <= self.warn_queue_size and queue.warned:
                # A previously warned queue got out of the warning threshold;
                # its owner should not be warned again.
                logger.warning("Queue '{}' was in warning zone but is OK now".format(
                q_name, q_size, self.del_queue_size))
                queue.warned = False
                self.back_to_normal_email(queue.owner, queue_data)

            # Commit any changes to the queue.
            db_session.add(queue)
            db_session.commit()

    def _exchange_from_queue(self, queue_data):
        exchange = 'could not be determined'
        detailed_data = self.api.queue(vhost=queue_data['vhost'],
                                       queue=queue_data['name'])
        if detailed_data['incoming']:
            exchange = detailed_data['incoming'][0]['exchange']['name']
        return exchange

    def warning_email(self, user, queue_data):
        if not self.emails:
            return

        exchange = self._exchange_from_queue(queue_data)

        subject = 'Pulse warning: queue "{}" is overgrowing'.format(
            queue_data['name'])
        body = '''Warning: your queue "{}" on exchange "{}" is
overgrowing ({} ready messages, {} total messages).

The queue will be automatically deleted when it exceeds {} messages.

Make sure your clients are running correctly and are cleaning up unused
durable queues.
'''.format(queue_data['name'], exchange, queue_data['messages_ready'],
           queue_data['messages'], self.del_queue_size)

        sendemail(subject=subject, from_addr=config.email_from,
                  to_addrs=[user.email], username=config.email_account,
                  password=config.email_password, text_data=body)

    def deletion_email(self, user, queue_data):
        if not self.emails:
            return

        exchange = self._exchange_from_queue(queue_data)

        subject = 'Pulse warning: queue "{}" has been deleted'.format(queue_data['name'])
        body = '''Your queue "{}" on exchange "{}" has been
deleted after exceeding the maximum number of unread messages.  Upon deletion
there were {} messages in the queue, out of a maximum {} messages.

Make sure your clients are running correctly and are cleaning up unused
durable queues.
'''.format(queue_data['name'], exchange, queue_data['messages'],
           self.del_queue_size)

        sendemail(subject=subject, from_addr=config.email_from,
                  to_addrs=[user.email], username=config.email_account,
                  password=config.email_password, text_data=body)

    def back_to_normal_email(self, user, queue_data):
        if not self.emails:
            return

        exchange = self._exchange_from_queue(queue_data)

        subject = 'Pulse warning: queue "{}" is back to normal'.format(queue_data['name'])
        body = '''Your queue "{}" on exchange "{}" is
now back to normal ({} ready messages, {} total messages).
'''.format(queue_data['name'], exchange, queue_data['messages_ready'],
           queue_data['messages'], self.del_queue_size)

        sendemail(subject=subject, from_addr=config.email_from,
                  to_addrs=[user.email], username=config.email_account,
                  password=config.email_password, text_data=body)

    def guard(self):
        logger.info("PulseGuardian started")
        while True:
            queues = self.api.queues()

            self.monitor_queues(queues)
            self.delete_zombie_queues(queues)

            time.sleep(config.polling_interval)


if __name__ == '__main__':
    # Initialize the database if necessary.
    init_db()

    api = PulseManagementAPI(host=config.rabbit_host,
                             management_port=config.rabbit_management_port,
                             user=config.rabbit_user,
                             password=config.rabbit_password)
    pulse_guardian = PulseGuardian(api)
    pulse_guardian.guard()
