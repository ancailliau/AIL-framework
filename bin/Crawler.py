#!/usr/bin/env python3
# -*-coding:UTF-8 -*

import os
import sys
import re
import redis
import datetime
import time
import subprocess
import requests

from pyfaup.faup import Faup

sys.path.append(os.environ['AIL_BIN'])
from Helper import Process
from pubsublogger import publisher

# ======== FUNCTIONS ========

def load_blacklist(service_type):
    try:
        with open(os.environ['AIL_BIN']+'/torcrawler/blacklist_{}.txt'.format(service_type), 'r') as f:
            redis_crawler.delete('blacklist_{}'.format(service_type))
            lines = f.read().splitlines()
            for line in lines:
                redis_crawler.sadd('blacklist_{}'.format(service_type), line)
    except Exception:
        pass

# Extract info form url (url, domain, domain url, ...)
def unpack_url(url):
    faup.decode(url)
    url_unpack = faup.get()
    domain = url_unpack['domain'].decode()
    if url_unpack['scheme'] is None:
        to_crawl['scheme'] = url_unpack['scheme']
        to_crawl['url']= 'http://{}'.format(url)
        to_crawl['domain_url'] = 'http://{}'.format(domain)
    else:
        to_crawl['scheme'] = url_unpack['scheme']
        to_crawl['url']= '{}://{}'.format(to_crawl['scheme'], url)
        to_crawl['domain_url'] = '{}://{}'.format(to_crawl['scheme'], domain)
    to_crawl['port'] = url_unpack['port']
    to_crawl['tld'] = url_unpack['tld'].deocode()
    return to_crawl

# get url, paste and service_type to crawl
def get_elem_to_crawl(rotation_mode):
    message = None
    domain_service_type = None

    #load_priority_queue
    for service_type in rotation_mode:
        message = redis_crawler.spop('{}_crawler_priority_queue'.format(service_type))
        if message is not None:
            domain_service_type = type_service
            break
    #load_normal_queue
    if message is None:
        for service_type in rotation_mode:
            message = redis_crawler.spop('{}_crawler_queue'.format(service_type))
            if message is not None:
                domain_service_type = type_service
                break

    if message:
        splitted = message.rsplit(';', 1)
        if len(splitted) == 2:
            url, paste = splitted
            if paste:
                paste = paste.replace(PASTES_FOLDER+'/', '')
        else:
            url = message
            paste = 'requested'

        message = {'url': url, 'paste': paste, 'type_service': domain_service_type, 'original_message': message}

    return message

def load_crawler_config(service_type, domain, paste):
    # Auto and Manual Crawling
    if paste is None:
        crawler_config['requested'] = True
    # default crawler
    else:
        crawler_config['requested'] = False
    return crawler_config

def is_domain_up_day(domain, type_service, date_day):
    if redis_crawler.sismember('{}_up:{}'.format(type_service, date_day), domain):
        return True
    else:
        return False

def set_crawled_domain_metadata(type_service, date, domain, father_item):
    # first seen
    if not redis_crawler.hexists('{}_metadata:{}'.format(type_service, domain), 'first_seen'):
        redis_crawler.hset('{}_metadata:{}'.format(type_service, domain), 'first_seen', date['date_day'])

    redis_crawler.hset('{}_metadata:{}'.format(type_service, domain), 'paste_parent', father_item)
    # last check
    redis_crawler.hset('{}_metadata:{}'.format(type_service, domain), 'last_check', date['date_day'])

# Put message back on queue
def on_error_send_message_back_in_queue(type_service, domain, message):
    if not redis_crawler.sismember('{}_domain_crawler_queue'.format(type_service), domain):
        redis_crawler.sadd('{}_domain_crawler_queue'.format(type_service), domain)
        redis_crawler.sadd('{}_crawler_priority_queue'.format(type_service), message)

##########################################################################################################<
def crawl_onion(url, domain, message, crawler_config):
    print('Launching Crawler: {}'.format(url))

    r_cache.hset('metadata_crawler:{}'.format(splash_port), 'crawling_domain', domain)
    r_cache.hset('metadata_crawler:{}'.format(splash_port), 'started_time', datetime.datetime.now().strftime("%Y/%m/%d  -  %H:%M.%S"))

    super_father = r_serv_metadata.hget('paste_metadata:'+paste, 'super_father')
    if super_father is None:
        super_father=paste

    retry = True
    nb_retry = 0
    while retry:
        try:
            r = requests.get(splash_url , timeout=30.0)
            retry = False
        except Exception:
            # TODO: relaunch docker or send error message
            nb_retry += 1

            if nb_retry == 6:
                on_error_send_message_back_in_queue(type_service, domain, message)
                publisher.error('{} SPASH DOWN'.format(splash_url))
                print('--------------------------------------')
                print('         \033[91m DOCKER SPLASH DOWN\033[0m')
                print('          {} DOWN'.format(splash_url))
                r_cache.hset('metadata_crawler:{}'.format(splash_port), 'status', 'SPLASH DOWN')
                nb_retry == 0

            print('         \033[91m DOCKER SPLASH NOT AVAILABLE\033[0m')
            print('          Retry({}) in 10 seconds'.format(nb_retry))
            time.sleep(10)

    if r.status_code == 200:
        r_cache.hset('metadata_crawler:{}'.format(splash_port), 'status', 'Crawling')
        process = subprocess.Popen(["python", './torcrawler/tor_crawler.py', splash_url, type_service, url, domain, paste, super_father],
                                   stdout=subprocess.PIPE)
        while process.poll() is None:
            time.sleep(1)

        if process.returncode == 0:
            output = process.stdout.read().decode()
            print(output)
            # error: splash:Connection to proxy refused
            if 'Connection to proxy refused' in output:
                on_error_send_message_back_in_queue(type_service, domain, message)
                publisher.error('{} SPASH, PROXY DOWN OR BAD CONFIGURATION'.format(splash_url))
                print('------------------------------------------------------------------------')
                print('         \033[91m SPLASH: Connection to proxy refused')
                print('')
                print('            PROXY DOWN OR BAD CONFIGURATION\033[0m'.format(splash_url))
                print('------------------------------------------------------------------------')
                r_cache.hset('metadata_crawler:{}'.format(splash_port), 'status', 'Error')
                exit(-2)
        else:
            print(process.stdout.read())
            exit(-1)
    else:
        on_error_send_message_back_in_queue(type_service, domain, message)
        print('--------------------------------------')
        print('         \033[91m DOCKER SPLASH DOWN\033[0m')
        print('          {} DOWN'.format(splash_url))
        r_cache.hset('metadata_crawler:{}'.format(splash_port), 'status', 'Crawling')
        exit(1)

# check external links (full_crawl)
def search_potential_source_domain(type_service, domain):
    external_domains = set()
    for link in redis_crawler.smembers('domain_{}_external_links:{}'.format(type_service, domain)):
        # unpack url
        url_data = unpack_url(link)
        if url_data['domain'] != domain:
            if url_data['tld'] == 'onion' or url_data['tld'] == 'i2p':
                external_domains.add(url_data['domain'])
    # # TODO: add special tag ?
    if len(external_domains) >= 20:
        redis_crawler.sadd('{}_potential_source'.format(type_service), domain)
        print('New potential source found: domain')
    redis_crawler.delete('domain_{}_external_links:{}'.format(type_service, domain))


if __name__ == '__main__':

    if len(sys.argv) != 3:
        print('usage:', 'Crawler.py', 'type_service (onion or i2p or regular)', 'splash_port')
        exit(1)
##################################################
    type_service = sys.argv[1]
    splash_port = sys.argv[2]

    rotation_mode = ['onion', 'regular']

    default_port = ['http': 80, 'https': 443]

################################################################### # TODO: port

    publisher.port = 6380
    publisher.channel = "Script"
    publisher.info("Script Crawler started")
    config_section = 'Crawler'

    # Setup the I/O queues
    p = Process(config_section)

    splash_url = '{}:{}'.format( p.config.get("Crawler", "splash_url_onion"),  splash_port)
    print('splash url: {}'.format(splash_url))

    faup = Faup()

    PASTES_FOLDER = os.path.join(os.environ['AIL_HOME'], p.config.get("Directories", "pastes"))

    r_serv_metadata = redis.StrictRedis(
        host=p.config.get("ARDB_Metadata", "host"),
        port=p.config.getint("ARDB_Metadata", "port"),
        db=p.config.getint("ARDB_Metadata", "db"),
        decode_responses=True)

    r_cache = redis.StrictRedis(
        host=p.config.get("Redis_Cache", "host"),
        port=p.config.getint("Redis_Cache", "port"),
        db=p.config.getint("Redis_Cache", "db"),
        decode_responses=True)

    redis_crawler = redis.StrictRedis(
        host=p.config.get("ARDB_Onion", "host"),
        port=p.config.getint("ARDB_Onion", "port"),
        db=p.config.getint("ARDB_Onion", "db"),
        decode_responses=True)

    # Track launched crawler
    r_cache.sadd('all_crawler', splash_port)
    r_cache.hset('metadata_crawler:{}'.format(splash_port), 'status', 'Waiting')
    r_cache.hset('metadata_crawler:{}'.format(splash_port), 'started_time', datetime.datetime.now().strftime("%Y/%m/%d  -  %H:%M.%S"))

    # update hardcoded blacklist
    load_blacklist('onion')
    load_blacklist('regular')

    while True:

        to_crawl = get_elem_to_crawl(rotation_mode)
        if to_crawl_dict:
            url_data = unpack_url(to_crawl['url'])
            # remove domain from queue
            redis_crawler.srem('{}_domain_crawler_queue'.format(to_crawl['type_service']), url_data['domain'])

            print()
            print()
            print('\033[92m------------------START CRAWLER------------------\033[0m')
            print('crawler type:     {}'.format(to_crawl['type_service']))
            print('\033[92m-------------------------------------------------\033[0m')
            print('url:         {}'.format(url_data['url']))
            print('domain:      {}'.format(url_data['domain']))
            print('domain_url:  {}'.format(url_data['domain_url']))

            # Check blacklist
            if not redis_crawler.sismember('blacklist_{}'.format(to_crawl['type_service']), url_data['domain'])):
                date = {'date_day'= datetime.datetime.now().strftime("%Y%m%d"),
                        'date_month'= datetime.datetime.now().strftime("%Y%m"),
                        'epoch'= int(time.time())}


                crawler_config = load_crawler_config(to_crawl['type_service'], url_data['domain'], to_crawl['paste'])
                # check if default crawler
                if not crawler_config['requested']:
                    # Auto crawl only if service not up this month
                    if redis_crawler.sismember('month_{}_up:{}'.format(to_crawl['type_service'], date['date_month']), url_data['domain']):
                        continue

                set_crawled_domain_metadata(to_crawl['type_service'], date, url_data['domain'], to_crawl['paste'], crawler_config)


                #### CRAWLER ####
                # Manual and Auto Crawler
                if crawler_config['requested']:

                ######################################################crawler strategy
                    # CRAWL domain
                    crawl_onion(url_data['url'], url_data['domain'], to_crawl['original_message'])

                # Default Crawler
                else:
                    # CRAWL domain
                    crawl_onion(url_data['domain_url'], url_data['domain'], to_crawl['original_message'])
                    if url != domain_url and not is_domain_up_day(url_data['domain'], to_crawl['type_service'], date['date_day']):
                        crawl_onion(url_data['url'], url_data['domain'], to_crawl['original_message'])


                        ################################################### handle port
                        # CRAWL with port
                        #if port is not None:
                        #    crawl_onion('{}:{}'.format(domain_url, port), domain, message)
                        ####         ####


                    # Save last_status day (DOWN)
                    if not is_domain_up_day(url_data['domain'], to_crawl['type_service'], date['date_day']):
                        redis_crawler.sadd('{}_down:{}'.format(to_crawl['type_service'], date['day']), url_data['domain'])

                    # if domain was UP at least one time
                    if redis_crawler.exists('crawler_history_{}:{}'.format(to_crawl['type_service'], url_data['domain'])):
                        # add crawler history (if domain is down)
                        if not redis_crawler.zrangebyscore('crawler_history_{}:{}'.format(to_crawl['type_service'], url_data['domain']), date['epoch'], date['epoch']):
                            # Domain is down
                            redis_crawler.zadd('crawler_history_{}:{}'.format(to_crawl['type_service'], url_data['domain']), int(date['epoch']), int(date['epoch']))

                        ############################
                        # extract page content
                        ############################

                    # update list, last crawled domains
                    redis_crawler.lpush('last_{}'.format(to_crawl['type_service']), url_data['domain'])
                    redis_crawler.ltrim('last_{}'.format(to_crawl['type_service']), 0, 15)

                    #update crawler status
                    r_cache.hset('metadata_crawler:{}'.format(splash_port), 'status', 'Waiting')
                    r_cache.hdel('metadata_crawler:{}'.format(splash_port), 'crawling_domain')
                else:
                    print('                 Blacklisted Domain')
                    print()
                    print()

            else:
                continue
        else:
            time.sleep(1)
