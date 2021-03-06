from __future__ import absolute_import

import csv
import datetime
import logging
import sys
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import click
import toolz
import yaml
import yaml.resolver
from click import echo
import json

from datacube.index.index import Index
from datacube.index.exceptions import MissingRecordError
from datacube.model import Dataset
from datacube.ui import click as ui
from datacube.ui.click import cli
from datacube.ui.common import get_metadata_path
from datacube.utils import read_documents, changes, InvalidDocException, SimpleDocNav
from datacube.utils.serialise import SafeDatacubeDumper
from datacube.model.utils import dedup_lineage, remap_lineage_doc, flatten_datasets
from datacube.utils.changes import get_doc_changes

from typing import Iterable

_LOG = logging.getLogger('datacube-dataset')


class BadMatch(Exception):
    pass


def report_old_options(mapping):
    def maybe_remap(s):
        if s in mapping:
            _LOG.warning("DEPRECATED option detected: --%s use --%s instead", s, mapping[s])
            return mapping[s]
        else:
            return s
    return maybe_remap


@cli.group(name='dataset', help='Dataset management commands')
def dataset_cmd():
    pass


def resolve_doc_files(paths, on_error):
    for p in paths:
        try:
            yield get_metadata_path(Path(p))
        except ValueError as e:
            on_error(p, e)


def doc_path_stream(files, on_error, uri=True):
    for fname in files:
        try:
            for p, doc in read_documents(fname, uri=uri):
                yield p, SimpleDocNav(doc)
        except InvalidDocException as e:
            on_error(fname, e)


def ui_doc_path_stream(paths):
    def on_error1(p, e):
        _LOG.error('No supported metadata docs found for dataset %s', p)

    def on_error2(p, e):
        _LOG.error('Failed reading documents from %s', p)

    yield from doc_path_stream(resolve_doc_files(paths, on_error=on_error1),
                               on_error=on_error2, uri=True)


def product_matcher(rules):
    assert len(rules) > 0

    def matches(doc, rule):
        return changes.contains(doc, rule.signature)

    def single_product_matcher(rule):
        def match(doc):
            if matches(doc, rule):
                return rule.product

            relevant_doc = {k: v for k, v in doc.items() if k in rule.signature}
            raise BadMatch('Dataset metadata did not match product signature.'
                           '\nDataset definition:\n %s\n'
                           '\nProduct signature:\n %s\n'
                           % (json.dumps(relevant_doc, indent=4),
                              json.dumps(rule.signature, indent=4)))

        return match

    if len(rules) == 1:
        return single_product_matcher(rules[0])

    def match(doc):
        matched = [rule.product for rule in rules if changes.contains(doc, rule.signature)]

        if len(matched) == 1:
            return matched[0]

        doc_id = doc.get('id', '<missing id>')

        if len(matched) == 0:
            raise BadMatch('No matching Product found for dataset %s' % doc_id)
        else:
            raise BadMatch('Auto match failed, dataset %s matches several products:\n  %s' % (
                doc_id,
                ','.join(p.name for p in matched)))

    return match


def check_dataset_consistent(dataset):
    """
    :type dataset: datacube.model.Dataset
    :return: (Is consistent, [error message|None])
    :rtype: (bool, str or None)
    """
    product_measurements = set(dataset.type.measurements.keys())

    if len(product_measurements) == 0:
        return True, None

    if dataset.measurements is None:
        return False, "No measurements defined for a dataset"

    # It the type expects measurements, ensure our dataset contains them all.
    if not product_measurements.issubset(dataset.measurements.keys()):
        return False, "measurement fields don't match type specification"

    return True, None


def load_rules_from_types(index, product_names=None):
    products = []
    if product_names:
        for name in product_names:
            product = index.products.get_by_name(name)
            if not product:
                _LOG.error('Supplied product name "%s" not present in the database', name)
                return None
            products.append(product)
    else:
        products += index.products.get_all()

    if len(products) == 0:
        _LOG.error('Found no products in the database')
        return None

    return [SimpleNamespace(product=p, signature=p.metadata_doc) for p in products]


def check_consistent(a, b):
    diffs = get_doc_changes(a, b)
    if len(diffs) == 0:
        return True, None

    def render_diff(offset, a, b):
        offset = '.'.join(map(str, offset))
        return '{}: {!r}!={!r}'.format(offset, a, b)

    return False, ", ".join([render_diff(offset, a, b) for offset, a, b in diffs])


def dataset_resolver(index,
                     product_matching_rules,
                     fail_on_missing_lineage=False,
                     verify_lineage=True,
                     skip_lineage=False):
    match_product = product_matcher(product_matching_rules)

    def resolve_no_lineage(ds, uri):
        doc = ds.doc_without_lineage_sources
        try:
            product = match_product(doc)
        except BadMatch as e:
            return None, e

        return Dataset(product, doc, uris=[uri], sources={}), None

    def resolve(main_ds, uri):
        try:
            main_ds = SimpleDocNav(dedup_lineage(main_ds))
        except InvalidDocException as e:
            return None, e

        main_uuid = main_ds.id

        ds_by_uuid = toolz.valmap(toolz.first, flatten_datasets(main_ds))
        all_uuid = list(ds_by_uuid)
        db_dss = {str(ds.id): ds for ds in index.datasets.bulk_get(all_uuid)}

        lineage_uuids = set(filter(lambda x: x != main_uuid, all_uuid))
        missing_lineage = lineage_uuids - set(db_dss)

        if missing_lineage and fail_on_missing_lineage:
            return None, "Following lineage datasets are missing from DB: %s" % (','.join(missing_lineage))

        if verify_lineage:
            bad_lineage = []

            for uuid in lineage_uuids:
                if uuid in db_dss:
                    ok, err = check_consistent(ds_by_uuid[uuid].doc_without_lineage_sources,
                                               db_dss[uuid].metadata_doc)
                    if not ok:
                        bad_lineage.append((uuid, err))

            if len(bad_lineage) > 0:
                error_report = '\n'.join('Inconsistent lineage dataset {}:\n> {}'.format(uuid, err)
                                         for uuid, err in bad_lineage)
                return None, error_report

        def with_cache(v, k, cache):
            cache[k] = v
            return v

        def resolve_ds(ds, sources, cache=None):
            cached = cache.get(ds.id)
            if cached is not None:
                return cached

            uris = [uri] if ds.id == main_uuid else []

            doc = ds.doc

            db_ds = db_dss.get(ds.id)
            if db_ds:
                product = db_ds.type
            else:
                product = match_product(doc)

            return with_cache(Dataset(product, doc, uris=uris, sources=sources), ds.id, cache)

        try:
            return remap_lineage_doc(main_ds, resolve_ds, cache={}), None
        except BadMatch as e:
            return None, e

    return resolve_no_lineage if skip_lineage else resolve


def load_datasets(dataset_paths, ds_resolve):
    for uri, ds in ui_doc_path_stream(dataset_paths):
        dataset, err = ds_resolve(ds, uri)

        if dataset is None:
            _LOG.error('%s', str(err))
            continue

        is_consistent, reason = check_dataset_consistent(dataset)
        if not is_consistent:
            _LOG.error("Dataset %s inconsistency: %s", dataset.id, reason)
            continue

        yield dataset


def load_datasets_for_update(dataset_paths, index):
    """Load datasets from disk, associate to a product by looking up existing
    dataset in the index. Datasets not in the database will be logged.

    Doesn't load lineage information

    Generates tuples in the form (new_dataset, existing_dataset)
    """
    def mk_dataset(ds, uri):
        uuid = ds.id

        if uuid is None:
            return None, None, "Metadata document it missing id field"

        existing = index.datasets.get(uuid)
        if existing is None:
            return None, None, "No such dataset in the database: {}".format(uuid)

        return Dataset(existing.type,
                       ds.doc_without_lineage_sources,
                       uris=[uri]), existing, None

    for uri, doc in ui_doc_path_stream(dataset_paths):
        dataset, existing, error_msg = mk_dataset(doc, uri)

        if dataset is None:
            _LOG.error("Failure while processing: %s\n" +
                       " > Reason: %s", uri, error_msg)
        else:
            is_consistent, reason = check_dataset_consistent(dataset)
            if is_consistent:
                yield dataset, existing
            else:
                _LOG.error("Dataset %s inconsistency: %s", dataset.id, reason)


@dataset_cmd.command('add',
                     help="Add datasets to the Data Cube",
                     context_settings=dict(token_normalize_func=report_old_options({
                         'dtype': 'product',
                         't': 'p'
                     })))
@click.option('--product', '-p', 'product_names',
              help=('Only match against products specified with this option, '
                    'you can supply several by repeating this option with a new product name'),
              multiple=True)
@click.option('--auto-match', '-a', help="Deprecated don't use it, it's a no-op",
              is_flag=True, default=False)
@click.option('--auto-add-lineage/--no-auto-add-lineage', is_flag=True, default=True,
              help=('Default behaviour is to automatically add lineage datasets if they are missing from the database, '
                    'but this can be disabled if lineage is expected to be present in the DB, '
                    'in this case add will abort when encountering missing lineage dataset'))
@click.option('--verify-lineage/--no-verify-lineage', is_flag=True, default=True,
              help=('Lineage referenced in the metadata document should be the same as in DB, '
                    'default behaviour is to skip those top-level datasets that have lineage data '
                    'different from the version in the DB. This option allows omitting verification step.'))
@click.option('--dry-run', help='Check if everything is ok', is_flag=True, default=False)
@click.option('--ignore-lineage',
              help="Pretend that there is no lineage data in the datasets being indexed",
              is_flag=True, default=False)
@click.option('--confirm-ignore-lineage',
              help="Pretend that there is no lineage data in the datasets being indexed, without confirmation",
              is_flag=True, default=False)
@click.argument('dataset-paths',
                type=click.Path(exists=True, readable=True, writable=False), nargs=-1)
@ui.pass_index()
def index_cmd(index, product_names,
              auto_match,
              auto_add_lineage,
              verify_lineage,
              dry_run,
              ignore_lineage,
              confirm_ignore_lineage,
              dataset_paths):

    if confirm_ignore_lineage is False and ignore_lineage is True:
        if sys.stdin.isatty():
            confirmed = click.confirm("Requested to skip lineage information, Are you sure?", default=False)
            if not confirmed:
                click.echo('OK aborting', err=True)
                sys.exit(1)
        else:
            click.echo("Use --confirm-ignore-lineage from non-interactive scripts. Aborting.")
            sys.exit(1)

        confirm_ignore_lineage = True

    if auto_match is True:
        _LOG.warning("--auto-match option is deprecated, update your scripts, behaviour is the same without it")

    rules = load_rules_from_types(index, product_names)
    if rules is None:
        sys.exit(2)

    assert len(rules) > 0

    ds_resolve = dataset_resolver(index, rules,
                                  skip_lineage=confirm_ignore_lineage,
                                  fail_on_missing_lineage=not auto_add_lineage,
                                  verify_lineage=verify_lineage)

    def run_it(dataset_paths):
        dss = load_datasets(dataset_paths, ds_resolve)
        index_datasets(dss,
                       index,
                       auto_add_lineage=auto_add_lineage,
                       dry_run=dry_run)

    # If outputting directly to terminal, show a progress bar.
    if sys.stdout.isatty():
        with click.progressbar(dataset_paths, label='Indexing datasets') as pp:
            run_it(pp)
    else:
        run_it(dataset_paths)


def index_datasets(dss, index, auto_add_lineage, dry_run):
    for dataset in dss:
        _LOG.info('Matched %s', dataset)
        if not dry_run:
            try:
                index.datasets.add(dataset, with_lineage=auto_add_lineage)
            except (ValueError, MissingRecordError) as e:
                _LOG.error('Failed to add dataset %s: %s', dataset.local_uri, e)


def parse_update_rules(keys_that_can_change):
    updates_allowed = {}
    for key_str in keys_that_can_change:
        updates_allowed[tuple(key_str.split('.'))] = changes.allow_any
    return updates_allowed


@dataset_cmd.command('update', help="Update datasets in the Data Cube")
@click.option('--allow-any', 'keys_that_can_change',
              help="Allow any changes to the specified key (a.b.c)",
              multiple=True)
@click.option('--dry-run', help='Check if everything is ok', is_flag=True, default=False)
@click.option('--location-policy',
              type=click.Choice(['keep', 'archive', 'forget']),
              default='keep',
              help='''What to do with previously recorded dataset location
'keep' - keep as alternative location [default]
'archive' - mark as archived
'forget' - remove from the index
''')
@click.argument('datasets',
                type=click.Path(exists=True, readable=True, writable=False), nargs=-1)
@ui.pass_index()
def update_cmd(index, keys_that_can_change, dry_run, location_policy, datasets):

    def loc_action(action, new_ds, existing_ds, action_name):
        if len(existing_ds.uris) == 0:
            return None

        if len(existing_ds.uris) > 1:
            _LOG.warning("Refusing to %s old location, there are several", action_name)
            return None

        new_uri, = new_ds.uris
        old_uri, = existing_ds.uris

        if new_uri == old_uri:
            return None

        if dry_run:
            echo('Will {} old location {}, and add new one {}'.format(action_name, old_uri, new_uri))
            return True

        return action(existing_ds.id, old_uri)

    def loc_archive(new_ds, existing_ds):
        return loc_action(index.datasets.archive_location, new_ds, existing_ds, 'archive')

    def loc_forget(new_ds, existing_ds):
        return loc_action(index.datasets.remove_location, new_ds, existing_ds, 'forget')

    def loc_keep(new_ds, existing_ds):
        return None

    update_loc = dict(archive=loc_archive,
                      forget=loc_forget,
                      keep=loc_keep)[location_policy]

    updates_allowed = parse_update_rules(keys_that_can_change)

    success, fail = 0, 0
    for dataset, existing_ds in load_datasets_for_update(datasets, index):
        _LOG.info('Matched %s', dataset)

        if location_policy != 'keep':
            if len(existing_ds.uris) > 1:
                # TODO:
                pass

        if not dry_run:
            try:
                index.datasets.update(dataset, updates_allowed=updates_allowed)
                update_loc(dataset, existing_ds)
                success += 1
                echo('Updated %s' % dataset.id)
            except ValueError as e:
                fail += 1
                echo('Failed to update %s: %s' % (dataset.id, e))
        else:
            if update_dry_run(index, updates_allowed, dataset):
                update_loc(dataset, existing_ds)
                success += 1
            else:
                fail += 1
    echo('%d successful, %d failed' % (success, fail))


def update_dry_run(index, updates_allowed, dataset):
    try:
        can_update, safe_changes, unsafe_changes = index.datasets.can_update(dataset, updates_allowed=updates_allowed)
    except ValueError as e:
        echo('Cannot update %s: %s' % (dataset.id, e))
        return False

    if can_update:
        echo('Can update %s: %s unsafe changes, %s safe changes' % (dataset.id,
                                                                    len(unsafe_changes),
                                                                    len(safe_changes)))
    else:
        echo('Cannot update %s: %s unsafe changes, %s safe changes' % (dataset.id,
                                                                       len(unsafe_changes),
                                                                       len(safe_changes)))
    return can_update


def build_dataset_info(index, dataset, show_sources=False, show_derived=False, depth=1, max_depth=99):
    # type: (Index, Dataset, bool) -> dict

    info = OrderedDict((
        ('id', str(dataset.id)),
        ('product', dataset.type.name),
        ('status', 'archived' if dataset.is_archived else 'active')
    ))

    # Optional when loading a dataset.
    if dataset.indexed_time is not None:
        info['indexed'] = dataset.indexed_time

    info['locations'] = dataset.uris
    info['fields'] = dataset.metadata.search_fields

    if depth < max_depth:
        if show_sources:
            info['sources'] = {key: build_dataset_info(index, source,
                                                       show_sources=True, show_derived=False,
                                                       depth=depth + 1, max_depth=max_depth)
                               for key, source in dataset.sources.items()}

        if show_derived:
            info['derived'] = [build_dataset_info(index, derived,
                                                  show_sources=False, show_derived=True,
                                                  depth=depth + 1, max_depth=max_depth)
                               for derived in index.datasets.get_derived(dataset.id)]

    return info


def _write_csv(infos):
    writer = csv.DictWriter(sys.stdout, ['id', 'status', 'product', 'location'], extrasaction='ignore')
    writer.writeheader()

    def add_first_location(row):
        locations_ = row['locations']
        row['location'] = locations_[0] if locations_ else None
        return row

    writer.writerows(add_first_location(row) for row in infos)


def _write_yaml(infos):
    """
    Dump yaml data with support for OrderedDicts.

    Allows for better human-readability of output: such as dataset ID field first, sources last.

    (Ordered dicts are output identically to normal yaml dicts: their order is purely for readability)
    """

    return yaml.dump_all(infos, sys.stdout, SafeDatacubeDumper, default_flow_style=False, indent=4)


_OUTPUT_WRITERS = {
    'csv': _write_csv,
    'yaml': _write_yaml,
}


@dataset_cmd.command('info', help="Display dataset information")
@click.option('--show-sources', help='Also show source datasets', is_flag=True, default=False)
@click.option('--show-derived', help='Also show derived datasets', is_flag=True, default=False)
@click.option('-f', help='Output format',
              type=click.Choice(list(_OUTPUT_WRITERS)), default='yaml', show_default=True)
@click.option('--max-depth',
              help='Maximum sources/derived depth to travel',
              type=int,
              # Unlikely to be hit, but will avoid total-death by circular-references.
              default=99)
@click.argument('ids', nargs=-1)
@ui.pass_index()
def info_cmd(index, show_sources, show_derived, f, max_depth, ids):
    # type: (Index, bool, bool, Iterable[str]) -> None

    # Using an array wrapper to get around the lack of "nonlocal" in py2
    missing_datasets = [0]

    def get_datasets(ids):
        for id_ in ids:
            dataset = index.datasets.get(id_, include_sources=show_sources)
            if dataset:
                yield dataset
            else:
                click.echo('%s missing' % id_, err=True)
                missing_datasets[0] += 1

    _OUTPUT_WRITERS[f](
        build_dataset_info(index,
                           dataset,
                           show_sources=show_sources,
                           show_derived=show_derived,
                           max_depth=max_depth)
        for dataset in get_datasets(ids)
    )
    sys.exit(missing_datasets[0])


@dataset_cmd.command('search')
@click.option('--limit', help='Limit the number of results',
              type=int, default=None)
@click.option('-f', help='Output format',
              type=click.Choice(list(_OUTPUT_WRITERS)), default='yaml', show_default=True)
@ui.parsed_search_expressions
@ui.pass_index()
def search_cmd(index, limit, f, expressions):
    """
    Search available Datasets
    """
    datasets = index.datasets.search(limit=limit, **expressions)
    _OUTPUT_WRITERS[f](
        build_dataset_info(index, dataset)
        for dataset in datasets
    )


def _get_derived_set(index, id_):
    """
    Get a single flat set of all derived datasets.
    (children, grandchildren, great-grandchildren...)
    """
    derived_set = {index.datasets.get(id_)}
    to_process = {id_}
    while to_process:
        derived = index.datasets.get_derived(to_process.pop())
        to_process.update(d.id for d in derived)
        derived_set.update(derived)
    return derived_set


@dataset_cmd.command('archive', help="Archive datasets")
@click.option('--archive-derived', '-d', help='Also recursively archive derived datasets', is_flag=True, default=False)
@click.option('--dry-run', help="Don't archive. Display datasets that would get archived",
              is_flag=True, default=False)
@click.argument('ids', nargs=-1)
@ui.pass_index()
def archive_cmd(index, archive_derived, dry_run, ids):
    for id_ in ids:
        to_process = _get_derived_set(index, id_) if archive_derived else [index.datasets.get(id_)]
        for d in to_process:
            click.echo('archiving %s %s %s' % (d.type.name, d.id, d.local_uri))
        if not dry_run:
            index.datasets.archive(d.id for d in to_process)


@dataset_cmd.command('restore', help="Restore datasets")
@click.option('--restore-derived', '-d', help='Also recursively restore derived datasets', is_flag=True, default=False)
@click.option('--dry-run', help="Don't restore. Display datasets that would get restored",
              is_flag=True, default=False)
@click.option('--derived-tolerance-seconds',
              help="Only restore derived datasets that were archived "
                   "this recently to the original dataset",
              default=10 * 60)
@click.argument('ids', nargs=-1)
@ui.pass_index()
def restore_cmd(index, restore_derived, derived_tolerance_seconds, dry_run, ids):
    tolerance = datetime.timedelta(seconds=derived_tolerance_seconds)

    for id_ in ids:
        _restore_one(dry_run, id_, index, restore_derived, tolerance)


def _restore_one(dry_run, id_, index, restore_derived, tolerance):
    """
    :type index: datacube.index.index.Index
    :type restore_derived: bool
    :type tolerance: datetime.timedelta
    :type dry_run:  bool
    :type id_: str
    """
    target_dataset = index.datasets.get(id_)
    to_process = _get_derived_set(index, id_) if restore_derived else {target_dataset}
    _LOG.debug("%s selected", len(to_process))

    # Only the already-archived ones.
    to_process = {d for d in to_process if d.is_archived}
    _LOG.debug("%s selected are archived", len(to_process))

    def within_tolerance(dataset):
        if not dataset.is_archived:
            return False
        t = target_dataset.archived_time
        return (t - tolerance) <= dataset.archived_time <= (t + tolerance)

    # Only those archived around the same time as the target.
    if restore_derived and target_dataset.is_archived:
        to_process = set(filter(within_tolerance, to_process))
        _LOG.debug("%s selected were archived within the tolerance", len(to_process))

    for d in to_process:
        click.echo('restoring %s %s %s' % (d.type.name, d.id, d.local_uri))
    if not dry_run:
        index.datasets.restore(d.id for d in to_process)
