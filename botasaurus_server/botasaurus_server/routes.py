from bottle import (
    request,
    response,
    post,
    get,
    route,
    redirect,
)
from .server import Server
from .apply_pagination import apply_pagination
from .filters import apply_filters
from .sorts import apply_sorts
from .views import _apply_view_for_ui, find_view
from .download import download_results
from .convert_to_english import convert_unicode_dict_to_ascii_dict
from .errors import JsonHTTPResponse, JsonHTTPResponseWithMessage, add_cors_headers
from .validation import (
    validate_task_request,
    validate_results_request,
    validate_download_params,
    is_valid_positive_integer,
    is_valid_positive_integer_including_zero,
    is_list_of_integers,
    validate_patch_task,
    validate_ui_patch_task,
)
from .routes_db import (
    perform_create_all_task,
    perform_create_tasks,
    perform_complete_task,
    is_task_done,
    queryTasks,
    get_task_from_db,
    perform_is_any_task_finished,
    perform_is_task_updated,
    perform_get_task_results,
    perform_download_task_results,
    perform_get_ui_task_results,
    perform_patch_task,
)
from .models import (
    Task,
    TaskStatus,
    isoformat,
    serialize_task,
    serialize_ui_display_task,
    serialize_ui_output_task,
)
from .task_helper import TaskHelper
from .task_results import TaskResults, create_cache_key
from casefy import kebabcase
from datetime import datetime, timezone
import json
import bottle

OK_MESSAGE = {"message": "OK"}

@route("/<:path>", method="OPTIONS")
def enable_cors_generic_route():
    """
    This route takes priority over all others. So any request with an OPTIONS
    method will be handled by this function.

    See: https://github.com/bottlepy/bottle/issues/402

    NOTE: This means we won't 404 any invalid path that is an OPTIONS request.
    """
    add_cors_headers(response.headers)

@bottle.hook("after_request")
def enable_cors_after_request_hook():
    """
    This executes after every route. We use it to attach CORS headers when
    applicable.
    """
    add_cors_headers(response.headers)

def jsonify(ls):
    response.content_type = "application/json"
    return json.dumps(ls)


def create_tasks(scraper, data, metadata, is_sync):
    create_all_tasks = scraper["create_all_task"]
    split_task = scraper["split_task"]
    scraper_name = scraper["scraper_name"]
    scraper_type = scraper["scraper_type"]
    get_task_name = scraper["get_task_name"]

    # create enough space
    all_task_sort_id = int(datetime.now(timezone.utc).timestamp()) * 10000
    all_task = None
    all_task_id = None

    if split_task:
        tasks_data = split_task(deep_clone_dict(data))
        if len(tasks_data) == 0:
            return [], [], split_task
    else:
        tasks_data = [data]
    if create_all_tasks:
        all_task, all_task_id = perform_create_all_task(
            data, metadata, is_sync, scraper_name, scraper_type, all_task_sort_id
        )

    def createTask(task_data, sort_id):
        task_name = get_task_name(task_data) if get_task_name else None
        return Task(
            status=TaskStatus.PENDING,
            scraper_name=scraper_name,
            task_name=task_name,
            scraper_type=scraper_type,
            is_all_task=False,
            is_sync=is_sync,
            parent_task_id=all_task_id,
            data=task_data,
            meta_data=metadata,
            sort_id=sort_id,  # Set the sort_id for the child task
        )

    def create_cached_tasks(task_datas):
        ls = []
        cache_keys = []

        for t in task_datas:
            key = create_cache_key(scraper_name, t)
            ls.append({"key": key, "task_data": t})
            cache_keys.append(key)

        cache_items_len, cache_map = create_cache_details(cache_keys)

        tasks = []

        def create_cached_task(task_data, cached_result, sort_id):
            now_time = datetime.now(timezone.utc)
            task_name = get_task_name(task_data) if get_task_name else None
            return Task(
                status=TaskStatus.COMPLETED,
                scraper_name=scraper_name,
                task_name=task_name,
                scraper_type=scraper_type,
                is_all_task=False,
                is_sync=is_sync,
                parent_task_id=all_task_id,
                data=task_data,
                meta_data=metadata,
                result_count=len(cached_result),
                started_at=now_time,
                finished_at=now_time,
                sort_id=sort_id,  # Set the sort_id for the cached task
            )

        cached_tasks = []
        for idx, item in enumerate(ls):
            cached_result = cache_map.get(item["key"])
            if cached_result:
                sort_id = all_task_sort_id - (idx + 1)
                ts = create_cached_task(item["task_data"], cached_result, sort_id)
                ts.result = cached_result
                tasks.append(ts)
                cached_tasks.append(ts)
            else:
                sort_id = all_task_sort_id - (idx + 1)
                tasks.append(createTask(item["task_data"], sort_id))

        return tasks, cached_tasks, cache_items_len

    if Server.cache:
        tasks, cached_tasks, cached_tasks_len = create_cached_tasks(tasks_data)
        tasks = perform_create_tasks(tasks, cached_tasks)
    else:
        tasks = []
        for idx, task_data in enumerate(tasks_data):
            # Assign sort_id for the non-cached task
            sort_id = all_task_sort_id - (idx + 1)
            tasks.append(createTask(task_data, sort_id))
        cached_tasks_len = 0

        tasks = perform_create_tasks(tasks)

    # here write results with help of cachemap
    if cached_tasks_len > 0:
        tasklen = len(tasks)
        if tasklen == cached_tasks_len:
            if all_task_id:
                first_started_at = cached_tasks[0].started_at
                with Session() as session:
                    TaskHelper.update_task(
                        session,
                        all_task_id,
                        {
                            "started_at": first_started_at,
                            "finished_at": first_started_at,
                        },
                    )
                    session.commit()
                all_task["started_at"] = isoformat(first_started_at)
                all_task["finished_at"] = isoformat(first_started_at)
            print("All Tasks Results are Returned from cache")
        else:
            print(
                f"{cached_tasks_len} out of {tasklen} Results are Returned from cache"
            )
        if all_task_id:
            perform_complete_task(
                all_task_id, Server.get_remove_duplicates_by(scraper_name)
            )

    tasks_with_all_task = tasks
    if all_task_id:
        tasks_with_all_task = [all_task] + tasks

    return tasks_with_all_task, tasks, split_task

def save(x):
    """Copy a file from source to destination."""
    TaskResults.save_task(
        x[0],
        x[1],
    )

def parallel_create_files(file_list):
    """

    Copy files in parallel.

    Parameters:
    file_list (list of dict): List of dictionaries with 'source_file' and 'destination_file' keys.
    """
    from joblib import Parallel, delayed

    Parallel(n_jobs=-1)(delayed(save)(file) for file in file_list)

def create_cache_details(cache_keys):
    existing_items = TaskResults.filter_items_in_cache(cache_keys)

    cache_items = TaskResults.get_cached_items_json_filed(existing_items)
    cache_items_len = len(cache_items)
    cache_map = {cache["key"]: cache["result"] for cache in cache_items}
    return cache_items_len, cache_map

@get("/")
def home():
    return redirect("/api")

@get("/api")
def get_api():
    return OK_MESSAGE

def create_async_task(validated_data):
    scraper_name, data, metadata = validated_data

    tasks_with_all_task, tasks, split_task = create_tasks(
        Server.get_scraper(scraper_name), data, metadata, False
    )

    if split_task:
        return tasks_with_all_task
    else:
        return tasks[0]

@post("/api/tasks/create-task-async")
def submit_async_task():

    json_data = request.json

    if isinstance(json_data, list):
        validated_data_items = [validate_task_request(item) for item in json_data]
        result = [
            create_async_task(validated_data_item)
            for validated_data_item in validated_data_items
        ]
        return jsonify(result)
    else:
        result = create_async_task(validate_task_request(request.json))
        return jsonify(result)

@retry_on_db_error
def refetch_tasks(item):
    with Session() as session:
        if isinstance(item, list):
            ids = [i["id"] for i in item]
            tasks = TaskHelper.get_tasks_by_ids(session, ids)
            return serialize(tasks)
        else:
            task = TaskHelper.get_task(session, item["id"])
            return serialize(task)

@post("/api/tasks/create-task-sync")
def submit_sync_task():
    json_data = request.json

    if isinstance(json_data, list):
        validated_data_items = [validate_task_request(item) for item in json_data]

        ts = []
        for validated_data_item in validated_data_items:
            scraper_name, data, metadata = validated_data_item
            ts.append(
                create_tasks(Server.get_scraper(scraper_name), data, metadata, True)
            )

        # wait for completion
        for t in ts:
            tasks_with_all_task, tasks, split_task = t

            if tasks_with_all_task and tasks_with_all_task[0]["is_all_task"]:
                wait_tasks = [tasks_with_all_task[0]]
            else:
                wait_tasks = tasks

            for task in wait_tasks:
                task_id = task["id"]
                while True:
                    if is_task_done(task_id):
                        break
                    sleep(0.1)
        # give results
        rst = []
        for t in ts:

            tasks_with_all_task, tasks, split_task = t

            if split_task:
                rst.append(refetch_tasks(tasks_with_all_task))
            else:
                rst.append(refetch_tasks(tasks[0]))

        return jsonify(rst)
    else:
        scraper_name, data, metadata = validate_task_request(request.json)

        tasks_with_all_task, tasks, split_task = create_tasks(
            Server.get_scraper(scraper_name), data, metadata, True
        )

        if tasks_with_all_task and tasks_with_all_task[0]["is_all_task"]:
            wait_tasks = [tasks_with_all_task[0]]
        else:
            wait_tasks = tasks

        for task in wait_tasks:
            task_id = task["id"]
            while True:
                if is_task_done(task_id):
                    break
                sleep(0.1)

        if split_task:
            return jsonify(refetch_tasks(tasks_with_all_task))
        else:
            return jsonify(refetch_tasks(tasks[0]))

def get_ets(with_results):
    if with_results:
        return [
            Task.id,
            Task.status,
            Task.task_name,
            Task.scraper_name,
            Task.result_count,
            Task.scraper_type,
            Task.is_all_task,
            Task.is_sync,
            Task.parent_task_id,
            Task.data,
            Task.meta_data,
            Task.finished_at,
            Task.started_at,
            Task.created_at,
            Task.updated_at,
        ]
    else:
        return [
            Task.id,
            Task.status,
            Task.task_name,
            Task.scraper_name,
            Task.result_count,
            Task.scraper_type,
            Task.is_all_task,
            Task.is_sync,
            Task.parent_task_id,
            Task.data,
            Task.meta_data,
            Task.finished_at,
            Task.started_at,
            Task.created_at,
            Task.updated_at,
        ]

def create_page_url(page, per_page, with_results):
    query_params = {}

    if page:
        query_params["page"] = page

    if per_page is not None:
        query_params["per_page"] = per_page

    if not with_results:
        query_params["with_results"] = False

    if query_params != {}:
        return query_params

@get("/api/tasks")
def get_tasks():
    with_results = request.query.get("with_results", "true").lower() == "true"
    page = request.query.get("page")
    per_page = request.query.get("per_page")

    if per_page is not None:
        if not is_valid_positive_integer(per_page):
            raise JsonHTTPResponseWithMessage(
                "Invalid 'per_page' parameter. It must be a positive integer."
            )
    else:
        page = 1
        per_page = None
    # Validate page and per_page
    if page is not None:
        # Check if any pagination parameter is provided
        if not is_valid_positive_integer(page):
            raise JsonHTTPResponseWithMessage(
                "Invalid 'page' parameter. It must be a positive integer."
            )
    else:
        page = 1

    return queryTasks(get_ets(with_results), with_results, page, per_page)

@get("/api/tasks/<task_id:int>")
def get_task(task_id):
    return get_task_from_db(task_id)

def is_valid_all_tasks(tasks):
    if not isinstance(tasks, list):
        return False

    for task in tasks:
        if not isinstance(task, dict):
            return False

        if not is_valid_positive_integer(task.get("id")):
            return False
        else:
            task["id"] = int(task["id"])

        if not is_valid_positive_integer_including_zero(task.get("result_count")):
            return False
        else:
            task["result_count"] = int(task["result_count"])
    return True

empty = {
    "count": 0,
    "total_pages": 0,
    "next": None,
    "previous": None,
}

def get_first_view(scraper_name):
    views = Server.get_view_ids(scraper_name)
    if views:
        return views[0]
    return None

def clean_results(
    scraper_name,
    results,
    input_data,
    filters,
    sort,
    view,
    page,
    per_page,
    result_count,
    contains_list_field,
):

    # Apply sorts, filters, and view
    results = apply_sorts(results, sort, Server.get_sorts(scraper_name))
    results = apply_filters(results, filters, Server.get_filters(scraper_name))
    results, hidden_fields = _apply_view_for_ui(
        results, view, Server.get_views(scraper_name), input_data
    )
    if contains_list_field or filters:
        result_count = len(results)
    results = apply_pagination(results, page, per_page, hidden_fields, result_count)
    return results

@post("/api/tasks/<task_id:int>/results")
def get_task_results(task_id):
    scraper_name, is_all_task, task_data, result_count = perform_get_task_results(
        task_id
    )
    validate_scraper_name(scraper_name)
    filters, sort, view, page, per_page = validate_results_request(
        request.json,
        Server.get_sort_ids(scraper_name),
        Server.get_view_ids(scraper_name),
        Server.get_default_sort(scraper_name),
    )
    contains_list_field, results = retrieve_task_results(
        task_id, scraper_name, is_all_task, view, filters, sort, page, per_page
    )
    if not isinstance(results, list):
        return jsonify({**empty, "results": results})

    results = clean_results(
        scraper_name,
        results,
        task_data,
        filters,
        sort,
        view,
        page,
        per_page,
        result_count,
        contains_list_field,
    )
    del results["hidden_fields"]
    return jsonify(results)

def generate_filename(task_id, view, is_all_task, task_name):
    if view:
        view = kebabcase(view)

    if is_all_task:
        if view:
            return f"all-task-{task_id}-{view}"  # Fixed missing f-string prefix
        else:
            return f"all-task-{task_id}"  # Fixed missing f-string prefix
    else:
        task_name = kebabcase(task_name)
        if view:
            return f"{task_name}-{view}"
        else:
            return f"{task_name}"

@post("/api/tasks/<task_id:int>/download")
def download_task_results(task_id):

    scraper_name, results, task_data, is_all_task, task_name = (
        perform_download_task_results(task_id)
    )
    validate_scraper_name(scraper_name)
    if not isinstance(results, list):
        raise JsonHTTPResponseWithMessage("No Results")

    fmt, filters, sort, view, convert_to_english = validate_download_params(
        request.json,
        Server.get_sort_ids(scraper_name),
        Server.get_view_ids(scraper_name),
        Server.get_default_sort(scraper_name),
    )

    # Apply sorts, filters, and view
    results = apply_sorts(results, sort, Server.get_sorts(scraper_name))
    results = apply_filters(results, filters, Server.get_filters(scraper_name))
    results, _ = _apply_view_for_ui(
        results, view, Server.get_views(scraper_name), task_data
    )

    if convert_to_english:
        results = convert_unicode_dict_to_ascii_dict(results)

    filename = generate_filename(task_id, view, is_all_task, task_name)

    return download_results(results, fmt, filename)

def delete_task(task_id, is_all_task, parent_id, remove_duplicates_by):
    fn = None
    with Session() as session:
        if is_all_task:
            TaskHelper.delete_child_tasks(session, task_id)
        else:
            if parent_id:
                all_children_count = TaskHelper.get_all_children_count(
                    session, parent_id, task_id
                )

                if all_children_count == 0:
                    TaskHelper.delete_task(session, parent_id, True)
                else:
                    has_executing_tasks = (
                        TaskHelper.get_pending_or_executing_child_count(
                            session, parent_id, task_id
                        )
                    )

                    if not has_executing_tasks:
                        aborted_children_count = TaskHelper.get_aborted_children_count(
                            session, parent_id, task_id
                        )

                        if aborted_children_count == all_children_count:
                            TaskHelper.abort_task(session, parent_id)
                        else:
                            failed_children_count = (
                                TaskHelper.get_failed_children_count(
                                    session, parent_id, task_id
                                )
                            )
                            if failed_children_count:
                                fn = lambda: TaskHelper.collect_and_save_all_task(
                                    parent_id,
                                    task_id,
                                    remove_duplicates_by,
                                    TaskStatus.FAILED,
                                )
                            else:
                                fn = lambda: TaskHelper.collect_and_save_all_task(
                                    parent_id,
                                    task_id,
                                    remove_duplicates_by,
                                    TaskStatus.COMPLETED,
                                )
        session.commit()
    if fn:
        fn()
    with Session() as session:
        TaskHelper.delete_task(session, task_id, is_all_task)
        session.commit()

def abort_task(task_id, is_all_task, parent_id, remove_duplicates_by):
    fn = None
    with Session() as session:
        if is_all_task:
            TaskHelper.abort_child_tasks(session, task_id)
        else:
            if parent_id:
                all_children_count = TaskHelper.get_all_children_count(
                    session, parent_id, task_id
                )

                if all_children_count == 0:
                    TaskHelper.abort_task(session, parent_id)
                else:
                    has_executing_tasks = (
                        TaskHelper.get_pending_or_executing_child_count(
                            session, parent_id, task_id
                        )
                    )

                    if not has_executing_tasks:
                        aborted_children_count = TaskHelper.get_aborted_children_count(
                            session, parent_id, task_id
                        )

                        if aborted_children_count == all_children_count:
                            TaskHelper.abort_task(session, parent_id)
                        else:
                            failed_children_count = (
                                TaskHelper.get_failed_children_count(
                                    session, parent_id, task_id
                                )
                            )
                            if failed_children_count:
                                fn = lambda: TaskHelper.collect_and_save_all_task(
                                    parent_id,
                                    task_id,
                                    remove_duplicates_by,
                                    TaskStatus.FAILED,
                                )
                            else:
                                fn = lambda: TaskHelper.collect_and_save_all_task(
                                    parent_id,
                                    task_id,
                                    remove_duplicates_by,
                                    TaskStatus.COMPLETED,
                                )
        session.commit()
    if fn:
        fn()
    with Session() as session:
        TaskHelper.abort_task(session, task_id)
        session.commit()

@route("/api/tasks/<task_id:int>/abort", method="PATCH")
def abort_single_task(task_id):
    perform_patch_task("abort", task_id)
    return OK_MESSAGE

@route("/api/tasks/<task_id:int>", method="DELETE")
def delete_single_task(task_id):
    perform_patch_task("delete", task_id)
    return OK_MESSAGE

@route("/api/tasks/bulk-abort", method="POST")
def bulk_abort_tasks():
    task_ids = validate_patch_task(request.json)

    for task_id in task_ids:
        perform_patch_task("abort", task_id)

    return OK_MESSAGE

@route("/api/tasks/bulk-delete", method="POST")
def bulk_delete_tasks():
    task_ids = validate_patch_task(request.json)

    for task_id in task_ids:
        perform_patch_task("delete", task_id)

    return OK_MESSAGE

@get("/api/ui/config")
def get_api_config():
    scrapers = Server.get_scrapers_config()
    config = Server.get_config()
    return {**config, "scrapers": scrapers}

@post("/api/ui/tasks/is-any-task-updated")  # Add this route
def is_any_task_finished():
    json_data = request.json

    if not is_list_of_integers(json_data.get("pending_task_ids")):
        raise JsonHTTPResponseWithMessage("'pending_task_ids' must be a list of integers")
    if not is_list_of_integers(json_data.get("progress_task_ids")):
        raise JsonHTTPResponseWithMessage("'progress_task_ids' must be a list of integers")

    if not is_valid_all_tasks(json_data.get("all_tasks")):
        raise JsonHTTPResponseWithMessage(
            "'all_tasks' must be a list of dictionaries with 'id' and 'result_count' keys"
        )

    pending_task_ids = json_data["pending_task_ids"]
    progress_task_ids = json_data["progress_task_ids"]
    all_tasks = json_data["all_tasks"]

    is_any_task_finished = perform_is_any_task_finished(
        pending_task_ids, progress_task_ids, all_tasks
    )

    return jsonify({"result": is_any_task_finished})

@post("/api/ui/tasks/is-task-updated")
def is_task_updated():
    # Extract 'task_id' and 'last_updated' from query parameters
    task_id = request.json.get("task_id")
    last_updated_str = request.json.get("last_updated")
    query_status = request.json.get("status")  # Extract the 'status' parameter

    # Validate 'task_id' using is_valid_integer
    if not is_valid_positive_integer(task_id):
        raise JsonHTTPResponseWithMessage("'task_id' must be a valid integer")

    # Validate 'task_id' using is_valid_integer
    if not is_string_of_min_length(query_status):
        raise JsonHTTPResponseWithMessage("'status' must be a string with at least one character")

    # Convert 'task_id' to integer
    task_id = int(task_id)

    # Parse 'last_updated' using fromisoformat
    try:
        last_updated = datetime.fromisoformat(last_updated_str)  # Strip 'Z' if present
        last_updated = last_updated.replace(
            tzinfo=None
        )  # Make 'last_updated' naive for comparison
    except ValueError:
        raise JsonHTTPResponseWithMessage("'last_updated' must be in valid ISO 8601 format")

    # Query the database for the task's 'updated_at' timestamp using the given 'task_id'
    task_data = perform_is_task_updated(task_id)

    if not task_data:
        raise create_task_not_found_error(task_id)

    task_updated_at, task_status = task_data
    task_updated_at = task_updated_at.replace(
        tzinfo=None
    )  # Make 'task_updated_at' naive for comparison

    if (task_updated_at > last_updated) or (task_status != query_status):
        is_updated = True
    else:
        is_updated = False

    return jsonify({"result":is_updated})

output_ui_tasks_ets = [
    Task.id,
    Task.status,
    Task.task_name,
    Task.result_count,
    Task.is_all_task,
    Task.finished_at,
    Task.started_at,
]

@get("/api/ui/tasks")
def get_ui_tasks():
    page = request.query.get("page")

    # Validate page and per_page
    if page is not None:
        # Check if any pagination parameter is provided
        if not is_valid_positive_integer(page):
            raise JsonHTTPResponseWithMessage(
                "Invalid 'page' parameter. It must be a positive integer."
            )
    else:
        page = 1

    return queryTasks(output_ui_tasks_ets, False, page, 100, serialize_ui_output_task)

@route("/api/ui/tasks", method="PATCH")
def patch_task():
    # This is done to return the tasks after the patch operation for purpose of faster ui response.
    page = request.query.get("page")
    if not (is_valid_positive_integer(page)):
        raise JsonHTTPResponseWithMessage(
            "Invalid 'page' parameter. Must be a positive integer.", status=400
        )

    action, task_ids = validate_ui_patch_task(request.json)

    for task_id in task_ids:
        perform_patch_task(action, task_id)

    return queryTasks(output_ui_tasks_ets, False, page, 100, serialize_ui_output_task)

@post("/api/ui/tasks/<task_id:int>/results")
def get_ui_task_results(task_id):

    scraper_name, is_all_task, serialized_task, task_data, result_count = (
        perform_get_ui_task_results(task_id)
    )
    validate_scraper_name(scraper_name)
    filters, sort, view, page, per_page = validate_results_request(
        request.json,
        Server.get_sort_ids(scraper_name),
        Server.get_view_ids(scraper_name),
        Server.get_default_sort(scraper_name),
    )
    forceApplyFirstView = (
        request.query.get("force_apply_first_view", "none").lower() == "true"
    )
    if forceApplyFirstView:
        view = get_first_view(scraper_name)

    contains_list_field, results = retrieve_task_results(
        task_id, scraper_name, is_all_task, view, filters, sort, page, per_page
    )

    if not isinstance(results, list):
        return jsonify({**empty, "results": results, "task": serialized_task})

    results = clean_results(
        scraper_name,
        results,
        task_data,
        filters,
        sort,
        view,
        page,
        per_page,
        result_count,
        contains_list_field,
    )
    results["task"] = serialized_task
    return jsonify(results)

def retrieve_task_results(
    task_id, scraper_name, is_all_task, view, filters, sort, page, per_page
):
    contains_list_field = (
        view and find_view(Server.get_views(scraper_name), view).contains_list_field
    )
    if is_all_task:
        if sort or filters or contains_list_field:
            # get all tasks we need to apply
            results = TaskResults.get_all_task(task_id)
        else:
            # if is listish view
            limit = page * per_page if per_page else None
            results = TaskResults.get_all_task(task_id, limit=limit)
    else:
        results = TaskResults.get_task(task_id)
    return contains_list_field, results
