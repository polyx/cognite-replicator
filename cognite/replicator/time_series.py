import logging
import time
from typing import Dict, List

from cognite.client import CogniteClient
from cognite.client.data_classes import TimeSeries

from . import replication


def create_time_series(
    src_ts: TimeSeries, src_dst_ids_assets: Dict[int, int], project_src: str, runtime: int
) -> TimeSeries:
    """
    Make a new copy of the time series to be replicated based on a source time series.

    Args:
        src_ts: The time series from the source to be replicated to the destination.
        src_dst_ids_assets: A dictionary of all the mappings of source asset id to destination asset id.
        project_src: The name of the project the object is being replicated from.
        runtime: The timestamp to be used in the new replicated metadata.

    Returns:
        The replicated time series to be created in the destination.
    """
    logging.debug(f"Creating a new time series based on source time series id {src_ts.id}")

    return TimeSeries(
        external_id=src_ts.external_id,
        name=src_ts.name,
        is_string=src_ts.is_string,
        metadata=replication.new_metadata(src_ts, project_src, runtime),
        unit=src_ts.unit,
        asset_id=replication.get_asset_ids([src_ts.asset_id], src_dst_ids_assets)[0] if src_ts.asset_id else None,
        is_step=src_ts.is_step,
        description=src_ts.description,
        security_categories=src_ts.security_categories,
        legacy_name=src_ts.external_id,
    )


def update_time_series(
    src_ts: TimeSeries, dst_ts: TimeSeries, src_dst_ids_assets: Dict[int, int], project_src: str, runtime: int
) -> TimeSeries:
    """
    Makes an updated version of the destination time series based on the corresponding source time series.

    Args:
        src_ts: The time series from the source to be replicated.
        dst_ts: The time series from the destination that needs to be updated to reflect changes made to its
                source time series.
        src_dst_ids_assets: A dictionary of all the mappings of source asset id to destination asset id.
        project_src: The name of the project the object is being replicated from.
        runtime: The timestamp to be used in the new replicated metadata.

    Returns:
        The updated time series object for the replication destination.
    """
    logging.debug(f"Updating existing time series {dst_ts.id} based on source time series id {src_ts.id}")

    dst_ts.external_id = src_ts.external_id
    dst_ts.name = src_ts.name
    dst_ts.is_string = src_ts.is_string
    dst_ts.metadata = replication.new_metadata(src_ts, project_src, runtime)
    dst_ts.unit = src_ts.unit
    dst_ts.asset_id = replication.get_asset_ids([src_ts.asset_id], src_dst_ids_assets)[0] if src_ts.asset_id else None
    dst_ts.is_step = src_ts.is_step
    dst_ts.description = src_ts.description
    dst_ts.security_categories = src_ts.security_categories
    return dst_ts


def filter_away_service_account_ts(ts_src: List[TimeSeries]) -> List[TimeSeries]:
    """
    Filter out the service account metrics time series so they won't be copied.

    Args:
        ts_src: A list of all the source time series.

    Returns:
        A list of time series that are not service account metrics.
    """
    return [ts for ts in ts_src if "service_account_metrics" not in ts.name]


def copy_ts(
    src_ts: List[TimeSeries],
    src_id_dst_ts: Dict[int, TimeSeries],
    src_dst_ids_assets: Dict[int, int],
    project_src: str,
    runtime: int,
    client: CogniteClient,
):
    """
    Creates/updates time series objects and then attempts to create and update these time series in the destination.

    Args:
        src_ts: A list of the time series that are in the source.
        src_id_dst_ts: A dictionary of a time series source id to it's matching destination object.
        src_dst_ids_assets: A dictionary of all the mappings of source asset id to destination asset id.
        project_src: The name of the project the object is being replicated from.
        runtime: The timestamp to be used in the new replicated metadata.
        client: The client corresponding to the destination project.

    """
    logging.info(f"Starting to replicate {len(src_ts)} time series.")
    create_ts, update_ts, unchanged_ts = replication.make_objects_batch(
        src_objects=src_ts,
        src_id_dst_obj=src_id_dst_ts,
        src_dst_ids_assets=src_dst_ids_assets,
        create=create_time_series,
        update=update_time_series,
        project_src=project_src,
        replicated_runtime=runtime,
    )

    logging.info(f"Creating {len(create_ts)} new time series and updating {len(update_ts)} existing time series.")

    if create_ts:
        logging.info(f"Creating {len(create_ts)} time series.")
        created_ts = replication.retry(client.time_series.create, create_ts)
        logging.info(f"Successfully created {len(created_ts)} time series.")

    if update_ts:
        logging.info(f"Updating {len(update_ts)} time series.")
        updated_ts = replication.retry(client.time_series.update, update_ts)
        logging.info(f"Successfully updated {len(updated_ts)} time series.")


def remove_not_replicated_in_dst(client_dst: CogniteClient) -> List[TimeSeries]:
    """
      Deleting all the time series in the destination that do not have the "_replicatedSource" in metadata, which means that is was not copied from the source, but created in the destination.

      Parameters:
         client_dst: The client corresponding to the destination project.


    """

    dst_list = client_dst.time_series.list(limit=None)

    not_copied_list = list()
    copied_list = list()
    for ts in dst_list:
        if ts.metadata and ts.metadata["_replicatedSource"]:
            copied_list.append(ts.id)

        else:
            not_copied_list.append(ts.id)

    client_dst.time_series.delete(id=not_copied_list)
    return not_copied_list


def remove_replicated_if_not_in_src(client_src: CogniteClient, client_dst: CogniteClient) -> List[TimeSeries]:
    """
      Compare the destination and source time series and delete the ones that are no longer in the source.

      Parameters:
        client_src: The client corresponding to the source project.
        client_dst: The client corresponding to the destination. project.


    """

    src_ids = {ts.id for ts in client_src.time_series.list(limit=None)}

    ts_dst_list = client_dst.time_series.list(limit=None)
    dst_id_list = {
        int(ts.metadata["_replicatedInternalId"]): ts.id
        for ts in ts_dst_list
        if ts.metadata and ts.metadata["_replicatedInternalId"]
    }

    diff_list = [dst_id for src_dst_id, dst_id in dst_id_list.items() if src_dst_id not in src_ids]
    client_dst.time_series.delete(id=diff_list)

    return diff_list


def replicate(
    client_src: CogniteClient,
    client_dst: CogniteClient,
    batch_size: int,
    num_threads: int = 1,
    delete_replicated_if_not_in_src: bool = False,
    delete_not_replicated_in_dst: bool = False,
):
    """
    Replicates all the time series from the source project into the destination project.

    Args:
        client_src: The client corresponding to the source project.
        client_dst: The client corresponding to the destination project.
        batch_size: The biggest batch size to post chunks in.
        num_threads: The number of threads to be used.
    """
    project_src = client_src.config.project
    project_dst = client_dst.config.project

    ts_src = client_src.time_series.list(limit=None)
    ts_dst = client_dst.time_series.list(limit=None)
    logging.info(f"There are {len(ts_src)} existing assets in source ({project_src}).")
    logging.info(f"There are {len(ts_dst)} existing assets in destination ({project_dst}).")

    src_id_dst_ts = replication.make_id_object_map(ts_dst)

    assets_dst = client_dst.assets.list(limit=None)
    src_dst_ids_assets = replication.existing_mapping(*assets_dst)
    logging.info(
        f"If a time series asset id is one of the {len(src_dst_ids_assets)} assets "
        f"that have been replicated then it will be linked."
    )

    ts_src_not_service = filter_away_service_account_ts(ts_src)
    logging.info(
        f"There are {(len(ts_src) - len(ts_src_not_service))} service"
        f"account metric time series that will not be copied."
    )

    replicated_runtime = int(time.time()) * 1000
    logging.info(f"These copied/updated time series will have a replicated run time of: {replicated_runtime}.")

    logging.info(
        f"Starting to copy and update {len(ts_src)} events from "
        f"source ({project_src}) to destination ({project_dst})."
    )

    if len(ts_src_not_service) > batch_size:
        replication.thread(
            num_threads=num_threads,
            copy=copy_ts,
            src_objects=ts_src_not_service,
            src_id_dst_obj=src_id_dst_ts,
            src_dst_ids_assets=src_dst_ids_assets,
            project_src=project_src,
            replicated_runtime=replicated_runtime,
            client=client_dst,
        )
    else:
        copy_ts(
            src_ts=ts_src_not_service,
            src_id_dst_ts=src_id_dst_ts,
            src_dst_ids_assets=src_dst_ids_assets,
            project_src=project_src,
            runtime=replicated_runtime,
            client=client_dst,
        )

    logging.info(
        f"Finished copying and updating {len(ts_src)} events from "
        f"source ({project_src}) to destination ({project_dst})."
    )

    if delete_replicated_if_not_in_src:
        asset_delete = remove_replicated_if_not_in_src(client_src, client_dst)
        logging.info(
            f"Deleted {len(asset_delete)} assets in destination ({project_dst})"
            f" because they were no longer in source ({project_src})   "
        )
    if delete_not_replicated_in_dst:
        asset_delete = remove_not_replicated_in_dst(client_dst)
        logging.info(
            f"Deleted {len(asset_delete)} assets in destination ({project_dst}) because"
            f"they were not replicated from source ({project_src})   "
        )
