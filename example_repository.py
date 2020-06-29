#!/usr/bin/env python
import click
import io
import os
import random
import shutil
import subprocess
import sys
import time

from aiida_repository.repository import Repository, FileType
from disk_objectstore import Container


def create_folder(base, node_repo, start_from=""):
    for obj in node_repo.list_objects(start_from):
        obj_relpath = os.path.join(start_from, obj.name)
        if obj.type == FileType.DIRECTORY:
            os.mkdir(os.path.join(base, obj_relpath))
            create_folder(base, node_repo, start_from=obj_relpath)
        elif obj.type == FileType.FILE:
            with node_repo.open(obj_relpath) as source_fhandle:
                # One could do streamed here
                with open(os.path.join(base, obj_relpath),
                          'wb') as dest_fhandle:
                    dest_fhandle.write(source_fhandle.read())
        else:
            raise RuntimeError("Unknown object type {}".format(obj.type))


def export_from_pack(
    extract_to,
    source_repo,
    node_uuids_groups,  # pylint: disable=too-many-locals
    print_space_statistics=False,
    compress=False,
    pack_size_target=4 * 1024 * 1024 * 1024):
    """Export the hashkeys, provided in groups, to the extract_to folder, directly into packs.

    This is a way to estimate the speed in re-exporting.
    """
    export_container_extract_to = os.path.join(extract_to, 'export-container')

    output_container = Container(folder=export_container_extract_to)
    output_container.init_container(pack_size_target=pack_size_target)

    start = time.time()
    for idx, node_uuids_chunk in enumerate(node_uuids_groups, start=1):
        obj_hashkeys = []
        for repo_node in source_repo.get_node_repositories(node_uuids_chunk):
            obj_hashkeys.extend(repo_node.get_all_obj_hashkeys())
        print("{} objects to write in phase {}".format(len(obj_hashkeys), idx))

        with source_repo.container.get_objects_stream_and_meta(
                obj_hashkeys) as triplets:
            ## NOTE! This does not work because the object streams yielded by the
            ## triplets generator must be consumed immediately,
            ## as they are then closed.
            ## old_obj_hashkeys = []
            # streams = []
            # for old_obj_hashkey, stream, _ in triplets:
            #     old_obj_hashkeys.append(old_obj_hashkey)
            #     streams.append(stream)
            # new_obj_hashkeys = output_container.add_streamed_objects_to_pack(
            #     streams, compress=compress)
            ## This is needed to recreate the metadata to put in the JSON
            ## I'm not doing it in this example
            # old_new_obj_hashkey_mapping = dict(zip(old_obj_hashkeys, new_obj_hashkeys))
            old_obj_hashkeys = []
            new_obj_hashkeys = []
            for old_obj_hashkey, stream, _ in triplets:
                old_obj_hashkeys.append(old_obj_hashkey)
                new_obj_hashkeys.append(
                    output_container.add_streamed_objects_to_pack(
                        [stream], compress=compress)[0])
            ## This is needed to recreate the metadata to put in the JSON
            ## I'm not doing it in this example
            old_new_obj_hashkey_mapping = dict(
                zip(old_obj_hashkeys, new_obj_hashkeys))

            # Print some size statistics
            if print_space_statistics:
                size_info = output_container.get_total_size()
                print("Output object store size info after phase {}:".format(
                    idx))
                for key in sorted(size_info.keys()):
                    print("- {:30s}: {}".format(key, size_info[key]))
    tot_time = time.time() - start
    print(
        "Time to store all objects (from packed to packed) in 2 steps: {:.3f} s"
        .format(tot_time))

    return output_container, old_new_obj_hashkey_mapping


def export_from_pack_grouped(
    extract_to,
    source_repo,
    node_uuids_groups,  # pylint: disable=too-many-locals,too-many-arguments
    print_space_statistics=False,
    compress=False,
    max_memory_usage=1000 * 1024 * 1024,
    pack_size_target=4 * 1024 * 1024 * 1024):
    """Export the uuids, provided in groups, to the extract_to folder, directly into packs.

    This is a way to estimate the speed in re-exporting.
    max_memory_usage is the number in bytes of the max allocation to make in memory
    """
    export_container_extract_to = os.path.join(extract_to, 'export-container')

    output_container = Container(folder=export_container_extract_to)
    output_container.init_container(pack_size_target=pack_size_target)

    write_time = 0.

    start = time.time()
    for idx, node_uuids_chunk in enumerate(node_uuids_groups, start=1):
        obj_hashkeys = []
        for repo_node in source_repo.get_node_repositories(node_uuids_chunk):
            obj_hashkeys.extend(repo_node.get_all_obj_hashkeys())
        print("{} objects to write in phase {}".format(len(obj_hashkeys), idx))

        with source_repo.container.get_objects_stream_and_meta(
                obj_hashkeys) as triplets:
            old_obj_hashkeys = []
            new_obj_hashkeys = []
            content_cache = {}
            cache_size = 0
            for old_obj_hashkey, stream, meta in triplets:
                # If the object itself is too big, just write it directly
                # via streams, bypassing completely the cache
                if meta['size'] > max_memory_usage:
                    print(
                        "DEBUG: DIRECT WRITE OF OBJECT (size={}, old-hashkey: {})"
                        .format(meta['size'], old_obj_hashkey))
                    old_obj_hashkeys.append(old_obj_hashkey)
                    write_start = time.time()
                    new_obj_hashkeys.append(
                        output_container.add_streamed_objects_to_pack(
                            [stream], compress=compress)[0])
                    write_time += time.time() - write_start
                # I were to read the content, I would be filling too much memory - I
                # flush the cache first
                elif cache_size + meta['size'] > max_memory_usage:
                    # Flush the cotnent of the cache
                    print(
                        "DEBUG: FLUSHING CACHE (current: {}, new: {}) while old-hashkey: {}"
                        .format(cache_size, meta['size'], old_obj_hashkey))
                    temp_old_hashkeys = []
                    stream_list = []
                    for old_cached_hashkey, cached_stream in content_cache.items(
                    ):
                        temp_old_hashkeys.append(old_cached_hashkey)
                        stream_list.append(cached_stream)
                    write_start = time.time()
                    temp_new_hashkeys = output_container.add_streamed_objects_to_pack(
                        stream_list, compress=compress)
                    write_time += time.time() - write_start
                    old_obj_hashkeys += temp_old_hashkeys
                    new_obj_hashkeys += temp_new_hashkeys
                    content_cache = {}
                    cache_size = 0
                    # I add this to the cache for the next round
                    content_cache[old_obj_hashkey] = io.BytesIO(stream.read())
                    cache_size += meta['size']
                # I can add this object to the memory cache, it is not too big.
                # I write it as a stream.
                else:
                    content_cache[old_obj_hashkey] = io.BytesIO(stream.read())
                    cache_size += meta['size']

            # The for loop is finished. Most probably I still have content in the
            # cache, just flush it
            print("DEBUG: FINAL CACHE FLUSH (size: {})".format(cache_size))
            temp_old_hashkeys = []
            stream_list = []
            for old_cached_hashkey, cached_stream in content_cache.items():
                temp_old_hashkeys.append(old_cached_hashkey)
                stream_list.append(cached_stream)
            write_start = time.time()
            temp_new_hashkeys = output_container.add_streamed_objects_to_pack(
                stream_list, compress=compress)
            write_time += time.time() - write_start
            old_obj_hashkeys += temp_old_hashkeys
            new_obj_hashkeys += temp_new_hashkeys
            content_cache = {}
            cache_size = 0

            old_new_obj_hashkey_mapping = dict(
                zip(old_obj_hashkeys, new_obj_hashkeys))

            # Print some size statistics
            if print_space_statistics:
                size_info = output_container.get_total_size()
                print("Output object store size info after phase {}:".format(
                    idx))
                for key in sorted(size_info.keys()):
                    print("- {:30s}: {}".format(key, size_info[key]))
    tot_time = time.time() - start
    print(
        "Time to store all objects (from packed to packed) in 2 steps: {:.3f} s (of which write-time: {:.3f} s)"
        .format(tot_time, write_time))

    return output_container, old_new_obj_hashkey_mapping


def import_from_legacy_repo(repo, node_folder, compress):

    print("*" * 74)
    print("* IMPORTING FROM LEGACY REPO")
    print(subprocess.check_output(['du', '-hs', node_folder]).decode('utf8'))

    folder_paths = {}

    for level1 in os.listdir(node_folder):
        if len(level1) != 2 or any(char not in "0123456789abcdef"
                                   for char in level1):
            continue
        for level2 in os.listdir(os.path.join(node_folder, level1)):
            if len(level2) != 2 or any(char not in "0123456789abcdef"
                                       for char in level2):
                continue
            for node_uuid_part in os.listdir(
                    os.path.join(node_folder, level1, level2)):
                node_uuid = level1 + level2 + node_uuid_part
                if len(node_uuid) != 36:
                    continue
                repo_node_folder = os.path.join(node_folder, level1, level2,
                                                node_uuid_part)
                #path_node_folder = os.path.join(repo_node_folder, 'path') # NOT TRUE for calcs (raw_inputs)
                #if not os.path.exists(path_node_folder):
                #    raise OSError("Path folder does not exist: {}".format(path_node_folder))
                folder_paths[node_uuid] = repo_node_folder

    # Create the new repo format (TIMING INSIDE THE FUNCTION)
    repo.create_repo_for_nodes(folder_paths=folder_paths, compress=compress)

    return folder_paths


@click.command()
@click.option(
    '-p',
    '--path',
    default='/tmp/test-container',
    help='The path to a test folder in which the container will be created.')
@click.option('-c',
              '--clear',
              is_flag=True,
              help='Clear the repository path folder before starting.')
@click.option('-U', '--db-user', required=True, help='DB user name.')
@click.option('-D',
              '--db-name',
              required=True,
              help='DB database name (IT WILL BE DROPPED! USE A TEST DB).')
@click.option('-P', '--db-password', required=True, help='DB password.')
@click.option(
    '-r',
    '--repository-folder',
    required=True,
    help='Repository folder of AiiDA to import (will not be modified).')
@click.option(
    '-x',
    '--extract-to',
    default='/tmp/test-repository-extract-to/',
    required=True,
    help=
    'Re-extract the repository to this folder. Must not exist unless -C is specified.'
)
@click.option('-C',
              '--clear-extract-to',
              is_flag=True,
              help='Delete the extract-to folder before starting.')
@click.option('-z',
              '--compress',
              is_flag=True,
              help='Use compression when packing.')
@click.option('-s',
              '--pack-size-target',
              type=int,
              default=4 * 1024 * 1024 * 1024,
              help='Target size for packs.')
@click.option(
    '-o',
    '--only',
    type=click.Choice([
        'load-legacy', 'export-new', 'export-new-to-legacy', 'rsync-legacy',
        'rsync-new'
    ]),
    required=False,
    help='Which parts of the script to run. Do not specify to run all.')
@click.help_option('-h', '--help')
def main(
    path,
    clear,  # pylint: disable=too-many-arguments,too-many-locals,too-many-statements,too-many-branches
    db_user,
    db_name,
    db_password,
    repository_folder,
    extract_to,
    clear_extract_to,
    compress,
    pack_size_target,
    only):

    repo = Repository(folder=path,
                      db_user=db_user,
                      db_name=db_name,
                      db_password=db_password,
                      pack_size_target=pack_size_target)
    print("Using a pack_size_target of {} ({} MB)".format(
        pack_size_target, (pack_size_target // 1024) // 1024))
    if clear_extract_to:
        if os.path.exists(extract_to):
            #input("Press ENTER to delete '{}'... ".format(extract_to))
            shutil.rmtree(extract_to)
    if os.path.exists(extract_to):
        print(
            "The folder '{}' exists - either delete it, or specify the -C option"
        )
        sys.exit(1)
    os.mkdir(extract_to)

    node_folder = os.path.join(repository_folder, 'node')

    if only is None or only == 'load-legacy':
        if clear:
            repo.drop_db()
            repo.container.init_container(clear=True,
                                          pack_size_target=pack_size_target)

        # Import from a legacy repository_folder to a new-style repo
        assert 'node' in os.listdir(
            repository_folder
        ), "No 'node' folder in repository_folder, is this an AiiDA repository?"
        import_from_legacy_repo(repo, node_folder, compress=compress)

        # Print some size statistics
        size_info = repo.container.get_total_size()
        print("Object store size info:")
        for key in sorted(size_info.keys()):
            print("- {:30s}: {}".format(key, size_info[key]))
        count = repo.container.count_objects()
        print("Object store objects info:")
        for key in sorted(count.keys()):
            print("- {:30s}: {}".format(key, count[key]))

    if only is None or only == 'export-new':

        start = time.time()
        node_uuids = list(repo.get_all_node_uuids())
        tot_time = time.time() - start
        print(
            "Time to get back all node UUIDs ({} received) from postgres: {:.3f} s"
            .format(len(node_uuids), tot_time))

        # Export everything in two chunks
        random.shuffle(node_uuids)

        node_uuids1, node_uuids2 = node_uuids[:len(node_uuids) //
                                              2], node_uuids[len(node_uuids) //
                                                             2:]

        print("*" * 74)
        print(
            "* REEXPORTING FROM NEW-STYLE REPO DIRECTLY TO NEW-STYLE PACKED REPO, IN A FEW CHUNKS"
        )

        #output_objectstore, old_new_mapping = export_from_pack(extract_to, repo, [node_uuids1, node_uuids2], compress=compress, pack_size_target=pack-size_target)
        output_objectstore, old_new_mapping = export_from_pack_grouped(
            extract_to,
            repo, [node_uuids1, node_uuids2],
            compress=compress,
            pack_size_target=pack_size_target)

        with repo.container.get_objects_stream_and_meta(
                old_new_mapping.keys()) as triplets:
            for old_hashkey, _, old_meta in triplets:
                # TODO: Not the fastest method, we should add get_object_size()
                new_size = len(
                    output_objectstore.get_object_content(
                        old_new_mapping[old_hashkey]))
                assert new_size == old_meta['size'], "{} ({}) vs {} ({})".format(
                    new_size, old_new_mapping[old_hashkey], old_meta['size'],
                    old_hashkey)

        # Print space statistics for exported
        size_info = output_objectstore.get_total_size()
        print("OUTPUT object store size info:")
        for key in sorted(size_info.keys()):
            print("- {:30s}: {}".format(key, size_info[key]))
        count = output_objectstore.count_objects()
        print("OUTPUT object store objects info:")
        for key in sorted(count.keys()):
            print("- {:30s}: {}".format(key, count[key]))

    if only is None or only == 'export-new-to-legacy':
        start = time.time()
        node_uuids = list(repo.get_all_node_uuids())
        tot_time = time.time() - start
        print(
            "Time to get back all node UUIDs ({} received) from postgres: {:.3f} s"
            .format(len(node_uuids), tot_time))

        # Let's try now to extract again
        random.shuffle(node_uuids)
        print("Extracting (shuffled) again in '{}'...".format(extract_to))
        start = time.time()
        node_repos = repo.get_node_repositories(node_uuids)
        tot_time = time.time() - start
        print(
            "Time to get back all folder metas for {} shuffled nodes from postgres: {:.3f} s"
            .format(len(node_uuids), tot_time))

        # Recreate the legacy repository format
        legacy_extract_to = os.path.join(extract_to, 'legacy')
        os.mkdir(legacy_extract_to)
        start = time.time()
        for node_repo in node_repos:
            repo_node_folder = os.path.join(legacy_extract_to,
                                            node_repo.node_uuid[:2],
                                            node_repo.node_uuid[2:4],
                                            node_repo.node_uuid[4:])
            os.makedirs(repo_node_folder)
            create_folder(base=repo_node_folder, node_repo=node_repo)
        tot_time = time.time() - start
        print(
            "Time to recreate the repository from new-style to legacy in '{}': {:.3f} s"
            .format(legacy_extract_to, tot_time))

        # Check that the two folders are identical
        try:
            output = subprocess.check_output(
                ['diff', '-rq', node_folder, legacy_extract_to])
        except subprocess.CalledProcessError as exc:
            print("ERROR! NON ZERO ERROR CODE. OUTPUT:")
            print(exc.output.decode('utf8'))
            sys.exit(1)
        if output:
            print("ERROR! FOLDERS DIFFER:")
            print(output.decode('utf8'))
            sys.exit(1)
        else:
            print("ALL OK! THE TWO FOLDERS ARE IDENTICAL!!")

    if only is None or only == 'rsync-legacy':
        legacy_export_extract_to = os.path.join(extract_to, 'legacy-export')
        start = time.time()
        print(
            subprocess.check_output([
                'rsync', '-aHx', node_folder + '/',
                legacy_export_extract_to + '/'
            ]).decode('utf8'))
        tot_time = time.time() - start
        print(
            "Time for the rsync of the legacy repo: {:.3f} s".format(tot_time))

        start = time.time()
        print(
            subprocess.check_output([
                'rsync', '-aHx', node_folder + '/',
                legacy_export_extract_to + '/'
            ]).decode('utf8'))
        tot_time = time.time() - start
        print("Time for the 2nd rsync of the legacy repo: {:.3f} s".format(
            tot_time))

    if only is None or only == 'rsync-new':
        new_rsync_extract_to = os.path.join(extract_to, 'rsync-new')
        start = time.time()
        print(
            subprocess.check_output([
                'rsync', '-aHx',
                repo.container.get_folder() + '/', new_rsync_extract_to + '/'
            ]).decode('utf8'))
        tot_time = time.time() - start
        print("Time for the rsync of the new-style repo: {:.3f} s".format(
            tot_time))

        # add a 1 kb file to a pack
        repo.container.add_objects_to_pack([b"a" * 1024])
        start = time.time()
        print(
            subprocess.check_output([
                'rsync', '-aHx',
                repo.container.get_folder() + '/', new_rsync_extract_to + '/'
            ]).decode('utf8'))
        tot_time = time.time() - start
        print(
            "Time for the 2nd rsync of the new-style repo after adding a 1kb file: {:.3f} s"
            .format(tot_time))

    print("All tests passed.")


if __name__ == "__main__":
    main()  # pylint: disable=no-value-for-parameter
