import json
import logging
import threading
import pickle
from typing import Dict, Callable, List

from hh_page_clf.model import DefaultModel
from kafka import KafkaConsumer, KafkaProducer
import pytest

from hh_deep_deep.service import Service, encode_model_data, decode_model_data
from hh_deep_deep.utils import configure_logging


configure_logging()


DEBUG = False


class ATestService(Service):
    queue_prefix = 'test-'
    jobs_prefix = 'tests'


def clear_topics():
    for service in [ATestService('trainer'), ATestService('crawler')]:
        topics = [
            service.input_topic,
            service.output_topic('progress'),
            service.output_topic('pages'),
        ]
        if service.queue_kind == 'trainer':
            topics.append(service.output_topic('model'))
        if service.queue_kind == 'crawler':
            topics.append(service.hints_input_topic)
        for topic in topics:
            consumer = KafkaConsumer(topic, consumer_timeout_ms=100,
                                     group_id='{}-group'.format(topic))
            for _ in consumer:
                pass
            consumer.commit()


@pytest.mark.slow
def test_service():
    # This is a big integration test, better run with "-s"

    debug('Clearing topics')
    clear_topics()
    job_id = 'test-id'
    ws_id = 'test-ws-id'
    producer = KafkaProducer(value_serializer=encode_message)

    def send(topic: str, message: Dict):
        producer.send(topic, message).get()
        producer.flush()

    start_crawler_message = _test_trainer_service(job_id, ws_id, send)

    _test_crawler_service(start_crawler_message, send)


def _test_trainer_service(job_id: str, ws_id: str,
                          send: Callable[[str, Dict], None]) -> Dict:
    """ Test trainer service, return start message for crawler service.
    """
    trainer_service = ATestService(
        'trainer', checkpoint_interval=50, check_updates_every=2, debug=DEBUG)
    progress_consumer, pages_consumer, model_consumer = [
        KafkaConsumer(trainer_service.output_topic(kind),
                      value_deserializer=decode_message)
        for kind in ['progress', 'pages', 'model']]
    trainer_service_thread = threading.Thread(target=trainer_service.run)
    trainer_service_thread.start()

    start_message = start_trainer_message(job_id, ws_id)
    debug('Sending start trainer message')
    send(trainer_service.input_topic, start_message)
    try:
        _check_progress_pages(progress_consumer, pages_consumer,
                              check_trainer=True)
        debug('Waiting for model, this might take a while...')
        model_message = next(model_consumer).value
        debug('Got it.')
        assert model_message['id'] == job_id
        link_model = model_message['link_model']

    finally:
        send(trainer_service.input_topic, stop_crawl_message(job_id))
        send(trainer_service.input_topic, {'from-tests': 'stop'})
        trainer_service_thread.join()

    start_message['link_model'] = link_model
    return start_message


def _check_progress_pages(progress_consumer, pages_consumer,
                          check_trainer=False):
    while True:
        debug('Waiting for pages message...')
        check_pages(next(pages_consumer))
        debug('Got it, now waiting for progress message...')
        progress_message = next(progress_consumer)
        debug('Got it:', progress_message.value.get('progress'))
        progress = check_progress(progress_message)
        if progress and (not check_trainer or
                         'Last deep-deep model checkpoint' in progress):
            return progress


def _test_crawler_service(
        start_message: Dict, send: Callable[[str, Dict], None]) -> None:
    start_message.update({
        'hints': start_message['seeds'][:1],
        'broadness': 'N10',
    })
    crawler_service = ATestService(
        'crawler', check_updates_every=2, max_workers=2, debug=DEBUG)
    progress_consumer, pages_consumer = [
        KafkaConsumer(crawler_service.output_topic(kind),
                      value_deserializer=decode_message)
        for kind in ['progress', 'pages']]
    crawler_service_thread = threading.Thread(target=crawler_service.run)
    crawler_service_thread.start()

    debug('Sending start crawler message')
    send(crawler_service.input_topic, start_message)
    debug('Sending additional hints')
    send(crawler_service.hints_input_topic, {
        'workspace_id': start_message['workspace_id'],
        'url': start_message['seeds'][1],
        'pinned': True,
    })
    try:
        progress = _check_progress_pages(progress_consumer, pages_consumer)
    finally:
        # TODO - check progress message
        send(crawler_service.input_topic, stop_crawl_message(start_message['id']))
        send(crawler_service.input_topic, {'from-tests': 'stop'})
        crawler_service_thread.join()


def debug(arg, *args: List[str]) -> None:
    print('{} '.format('>' * 60), arg, *args)


def check_progress(message):
    value = message.value
    assert value['id'] == 'test-id'
    progress = value['progress']
    if progress not in {
            'Craw is not running yet', 'Crawl started, no updates yet'}:
        assert 'pages processed' in progress
        assert 'domains' in progress
        assert 'relevant' in progress
        assert 'average score' in progress.lower()
        return progress


def check_pages(message):
    value = message.value
    assert value['id'] == 'test-id'
    page_sample = value['page_sample']
    assert len(page_sample) >= 1
    for s in page_sample:
        assert isinstance(s['score'], float)
        assert 100 >= s['score'] >= 0
        assert s['url'].startswith('http')


def start_trainer_message(id_: str, ws_id: str) -> Dict:
    model = DefaultModel()
    model.fit(
        [{'url': 'http://a.com', 'text': text}
         for text in ['a good day', 'feeling nice today', 'it is sunny',
                      'what a mess', 'who invented it', 'so boring', 'stupid']],
        [1, 1, 1, 0, 0, 0, 0])
    return {
        'id': id_,
        'workspace_id': ws_id,
        'page_model': encode_model_data(pickle.dumps(model)),
        'seeds': ['http://wikipedia.org', 'http://news.ycombinator.com'],
    }


def stop_crawl_message(id_: str) -> Dict:
    return {'id': id_, 'stop': True, 'verbose': True}


def test_encode_model():
    data = 'ё'.encode('utf8')
    assert isinstance(encode_model_data(data), str)
    assert data == decode_model_data(encode_model_data(data))
    assert decode_model_data(None) is None
    assert encode_model_data(None) is None


# TODO - test that encode_model_data and encode_object
# from hh_page_classifier are in sync


def decode_message(message: bytes) -> Dict:
    try:
        return json.loads(message.decode('utf8'))
    except Exception as e:
        logging.error('Error deserializing message', exc_info=e)
        raise


def encode_message(message: Dict) -> bytes:
    try:
        return json.dumps(message).encode('utf8')
    except Exception as e:
        logging.error('Error serializing message', exc_info=e)
        raise
