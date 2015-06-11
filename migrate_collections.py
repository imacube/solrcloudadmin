"""
Migrate collections from one host to another.
"""

import logging
import time

LOGGER = None

def load_json_file(json_file):
    """
    Load the cluster data from a JSON file.
    """
    import json
    with open(json_file) as data_file:
        data = json.load(data_file)
    return data

def get_collection_data(solr_cloud=None, json_file=None):
    """
    Load the collection data either from a file
    or from the SolrCloud cluster.
    """
    if json_file:
        return load_json_file(json_file=json_file)
    elif solr_cloud:
        return solr_cloud.list_collection_data()

def check_node_live(solr_cloud, collection, shard, node_to_check):
    """
    Pulls the current collection state.json and checks if the destination_node
    node already has a copy of the shard.
    """
    if node_to_check is None:
        return False
    shard_data = solr_cloud.get_collection_state(collection=collection)['shards'][shard]
    for replica, core in shard_data['replicas'].items():
        LOGGER.debug('replica=%s', replica)
        LOGGER.debug('node_name=%s', core['node_name'])
        LOGGER.debug('node_to_check=%s', node_to_check)
        if node_to_check == core['node_name']:
            return True
    return False

def check_node_snapshot(shard_data, node_to_check):
    """
    Uses the snapshot of the SolrCloud cluster state.json files and checks if
    the destination_node node already has a copy of the shard.
    """
    if node_to_check is None:
        return False
    for replica, core in shard_data['replicas'].items():
        LOGGER.debug('replica=%s', replica)
        LOGGER.debug('node_name=%s', core['node_name'])
        LOGGER.debug('node_to_check=%s', node_to_check)
        if node_to_check == core['node_name']:
            return True
    return False


def wait_for_async(solr_cloud, async):
    """
    Wait for an async job to finish.
    """
    failed_queries = 0
    while True:
        if failed_queries > 10:
            LOGGER.critical('failed_queries=%d > 10', failed_queries)
            return False
        response = solr_cloud.get_request_status(async)
        LOGGER.debug('response = %s', response)
        if response['status']['state'] == 'running':
            LOGGER.debug('Waiting for job to finish...')
            failed_queries = 0
        elif response['status']['state'] == 'submitted':
            LOGGER.debug('Waiting for job to start...')
            failed_queries = 0
        elif response['status']['state'] == 'notfound':
            LOGGER.warn('Did not find task for async ID %d.', async)
            failed_queries += 1
        elif response['status']['state'] == 'completed':
            return True
        else:
            LOGGER.critical('Unexpected task state, response = %s', response)
            return False
        time.sleep(2)

def add_replicas(
        solr_cloud,
        collection_list,
        source_node,
        destination_node=None,
        limit=0,
        dryrun=False
    ):
    """
    Add replicas for collections on source node.

    If destination_node is specified then collections will be added to it.
    """
    if dryrun:
        LOGGER.warn('This is a dry run!')
    LOGGER.debug('dryrun=%s', dryrun)

    # Reset request ID
    response = solr_cloud.get_request_status('-1')
    LOGGER.debug('response=%s', response)
    if response['responseHeader']['status'] != 0:
        LOGGER.critical(
            'Cleaning up stored states failed, response=%s',
            response
            )
        return False

    # Set starting point for request IDs
    request_id = 1000

    count = 0
    for collection, value in collection_list.items():
        LOGGER.debug('loop count=%d', count)
        LOGGER.debug('collection=%s', collection)
        LOGGER.debug(
            'collection dict=%s',
            value
            )
        for shard, data in value['shards'].items():
            LOGGER.debug('shard=%s', shard)
            LOGGER.debug('data=%s', data)
            # Check if source node has a copy of the collection, shard
            if not check_node_snapshot(shard_data=data, node_to_check=source_node):
                LOGGER.debug(
                    'Snapshot of state.json does not list a replica \
for collection=%s on source_node=%s for shard=%s',
                    collection,
                    source_node,
                    shard
                    )
                continue
            # Check if the destination node has a copy of the collection, shard
            if check_node_snapshot(shard_data=data, node_to_check=destination_node):
                LOGGER.warn(
                    'Snapshot of state.json lists a replica for \
collection=%s, shard=%s, on the destination_node=%s',
                    collection,
                    shard,
                    destination_node
                    )
                continue
            # Check if the live destination node has the source shard
            if check_node_live(
                    solr_cloud=solr_cloud,
                    collection=collection,
                    shard=shard,
                    node_to_check=destination_node
                ):
                LOGGER.warn(
                    'Live state.json check lists a replica for \
collection=%s, shard=%s on the destination_node=%s',
                    collection,
                    shard,
                    destination_node
                    )
                continue

            LOGGER.info(
                'Adding replica for collection=%s, shard=%s, destination_node=%s',
                collection,
                shard,
                destination_node
                )
            count += 1

            if dryrun:
                continue

            response = solr_cloud.add_replica(
                collection=collection,
                shard=shard,
                node=destination_node,
                async=request_id
                )
            LOGGER.debug('response=%s', response)
            if response['responseHeader']['status'] != 0 or 'error' in response:
                LOGGER.error(
                    'Unable to add replica for collection=%s, \
shard=%s, destination_node=%s. response=%s',
                    collection,
                    shard,
                    destination_node,
                    response
                    )
                continue
            if wait_for_async(solr_cloud, request_id):
                LOGGER.info(
                    'Successfully added replica for \
collection=%s, shard=%s, destination_node=%s',
                    collection,
                    shard,
                    destination_node
                    )
                request_id += 1
            else:
                LOGGER.critical(
                    'Unable to add replica, check response from wait_for_async.'
                    )
                return False

        if limit > 0 and count >= limit:
            break
    return True

def delete_replicas(
        solr_cloud,
        collection_list,
        source_node,
        limit=0,
        dryrun=False
    ):
    """
    Del replicas for collections on source node.
    """
    if dryrun:
        LOGGER.warn('This is a dry run!')
    LOGGER.debug('dryrun=%s', dryrun)

    count = 0
    for collection, value in collection_list.items():
        LOGGER.debug('loop count=%d', count)
        LOGGER.debug('collection=%s', collection)
        LOGGER.debug(
            'collection dict=%s',
            value
            )
        replication_factor = int(value['replicationFactor'])
        for shard, data in value['shards'].items():
            LOGGER.debug('shard=%s', shard)
            LOGGER.debug('data=%s', data)
            # Check if source node has a copy of the collection, shard
            if not check_node_snapshot(
                    shard_data=data,
                    node_to_check=source_node
                ):
                LOGGER.debug('No replica on source_node for shard=%s', shard)
                continue
            if not check_node_live(
                    solr_cloud=solr_cloud,
                    collection=collection,
                    shard=shard,
                    node_to_check=source_node
                ):
                LOGGER.warn(
                    'Live state.json check does not list a replica for \
collection=%s, shard=%s on the source_node=%s',
                    collection,
                    shard,
                    source_node
                    )
                continue

            # Check if replica count os greater then 1 and above replication_factor
            LOGGER.debug(
                'Replica count of collection=%s, shard=%s is %d',
                collection,
                shard,
                len(data['replicas'])
                )
            if len(data['replicas']) <= 1:
                LOGGER.error(
                    'Will not delete final replica of collection=%s, shard=%s, \
replica count is %d <= 1',
                    collection,
                    shard,
                    len(data['replicas'])
                )
                continue
            elif len(data['replicas']) <= replication_factor:
                LOGGER.error(
                    'Will not delete replica of collection=%s, shard=%s, \
when replica count below or equal to replication factor, \
count of replicas=%d <= replication_factor=%d',
                    collection,
                    shard,
                    len(data['replicas']),
                    replication_factor
                )
                continue

            # Find replica to delete
            for replica_name, core in data['replicas'].items():
                if core['node_name'] == source_node:
                    replica = replica_name

            if not replica:
                LOGGER.warn(
                    'Unable to find replica for collection=%s, shard=%s',
                    collection,
                    shard
                    )
                continue

            LOGGER.info(
                'Deleting replica=%s in collection=%s, shard=%s',
                replica,
                collection,
                shard
                )
            count += 1

            if dryrun:
                continue

            response = solr_cloud.delete_replica(
                collection=collection,
                shard=shard,
                replica=replica
                )
            LOGGER.debug('response=%s', response)
            if response['responseHeader']['status'] != 0 or 'error' in response:
                LOGGER.error(
                    'Unable to delete replica=%s for collection=%s, \
shard=%s. response=%s',
                    replica,
                    collection,
                    shard,
                    response
                    )

        if limit > 0 and count >= limit:
            break
    return True

def parse_arguments():
    """
    Parse command line arguments.
    """
    import argparse
    parser = argparse.ArgumentParser(
        description='Migrates collections from one node to another.'
        )
    parser.add_argument(
        '--url', nargs=1, dest='url', required=True,
        type=str,
        help="""The hostname or IP of the SolrCloud cluster, e.g. solrcloud:8983/solr"""
        )
    parser.add_argument(
        '--source_node', nargs=1, dest='source_node', required=True,
        type=str,
        help="""The source host as SolrCloud formated node_name."""
        )
    parser.add_argument(
        '--destination_node', nargs=1, dest='destination_node', required=False,
        type=str,
        help="""The destination host as SolrCloud formated node_name."""
        )
    parser.add_argument(
        '--limit', nargs=1, dest='limit', required=False,
        type=int,
        help="""The limit of how many collections to move. Defaults to all."""
        )
    parser.add_argument(
        '--add', action='store_true', required=False,
        help="""Add replicas for collections found on the source node."""
        )
    parser.add_argument(
        '--delete', action='store_true', required=False,
        help="""Delete replicas from a collections found on the source node."""
        )
    parser.add_argument(
        '--migrate', action='store_true', required=False,
        help="""Add collections from source to destination node and delete them from source node."""
        )
    parser.add_argument(
        '--dry_run', action='store_true', required=False,
        help="""Simulate running."""
        )
    parser.add_argument(
        '--json_file', nargs=1, dest='json_file', required=False,
        help="""Read in collection list data from file (JSON format)."""
        )
    parser.add_argument(
        '--debug', action='store_true', required=False,
        help="""Turn on debug logging."""
        )
    parser.add_argument(
        '--local_solrcloudadmin', action='store_true', required=False,
        help="""Import SolrCloudAdmin from local directory."""
        )
    return parser.parse_args()

def configure_logging(logger_name, log_level=logging.INFO):
    """
    Configure logging for this script.
    :args log_level: logging level to set
    """
    global LOGGER
    LOGGER = logging.getLogger(logger_name)
    LOGGER.setLevel(log_level)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s.%(funcName)s, line %(lineno)d - \
%(levelname)s - %(message)s'
        )
    console_handler.setFormatter(formatter)
    LOGGER.addHandler(console_handler)
    LOGGER.debug('Starting init of %s', logger_name)

def main():
    """
    Called if run from command line.
    """
    args = parse_arguments()

    if args.local_solrcloudadmin:
        import sys
        sys.path.append('solrcloudadmin')
        from solrcloudadmin import SolrCloudAdmin

    if args.debug:
        configure_logging(logger_name='migrate_collections', log_level=logging.DEBUG)
        solr_cloud = SolrCloudAdmin(
            url=args.url[0],
            log_level=logging.INFO,
            max_retries=3,
            retry_sleeptime=10
            )
    else:
        configure_logging(logger_name='migrate_collections', log_level=logging.INFO)
        solr_cloud = SolrCloudAdmin(
            url=args.url[0],
            log_level=logging.INFO,
            max_retries=3,
            retry_sleeptime=10
            )

    source_node = args.source_node[0]

    if 'destination_node' in vars(args) and args.destination_node:
        destination_node = args.destination_node[0]
        LOGGER.debug('destination_node: %s', destination_node)
    else:
        destination_node = None

    if 'limit' in vars(args) and args.limit:
        limit = args.limit[0]
        LOGGER.debug('limit: %s', limit)
    else:
        limit = 0

    if 'json_file' in vars(args) and args.json_file:
        json_file = args.json_file[0]
    else:
        json_file = None

    if args.dry_run:
        dryrun = True
    else:
        dryrun = False

    if args.add and args.delete:
        LOGGER.critical('Can only use --add or --delete, not both')
        return

    collection_list = get_collection_data(solr_cloud=solr_cloud, json_file=json_file)

    if args.add:
        return_value = add_replicas(
            solr_cloud,
            collection_list,
            source_node,
            destination_node,
            limit,
            dryrun
            )
        LOGGER.info('add_replicas returned %s', return_value)
    elif args.delete:
        return_value = delete_replicas(
            solr_cloud,
            collection_list,
            source_node,
            limit,
            dryrun
            )
        LOGGER.info('delete_replicas returned %s', return_value)
    elif args.migrate:
        return_value = add_replicas(
            solr_cloud,
            collection_list,
            source_node,
            destination_node,
            limit,
            dryrun
            )
        LOGGER.info('add_replicas returned %s', return_value)
        LOGGER.info('Updating collection_list if needed')
        collection_list = get_collection_data(solr_cloud=solr_cloud, json_file=json_file)
        return_value = delete_replicas(
            solr_cloud,
            collection_list,
            source_node,
            limit,
            dryrun
            )
        LOGGER.info('delete_replicas returned %s', return_value)

if __name__ == '__main__':
    main()
