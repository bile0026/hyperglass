"""Hyperglass Front End"""

# Standard Library Imports
import time
from ast import literal_eval
from pathlib import Path

# Third Party Imports
import aredis
from logzero import logger
from prometheus_client import CollectorRegistry
from prometheus_client import Counter
from prometheus_client import generate_latest
from prometheus_client import multiprocess
from prometheus_client import CONTENT_TYPE_LATEST
from sanic import Sanic
from sanic import response
from sanic.exceptions import NotFound
from sanic.exceptions import ServerError
from sanic.exceptions import InvalidUsage
from sanic_limiter import Limiter
from sanic_limiter import RateLimitExceeded
from sanic_limiter import get_remote_address

# Project Imports
from hyperglass.render import render_html
from hyperglass.command.execute import Execute
from hyperglass.configuration import devices
from hyperglass.configuration import logzero_config  # noqa: F401
from hyperglass.configuration import params
from hyperglass.constants import Supported
from hyperglass.constants import code
from hyperglass.exceptions import (
    HyperglassError,
    AuthError,
    ScrapeError,
    RestError,
    InputInvalid,
    InputNotAllowed,
)

logger.debug(f"Configuration Parameters:\n {params.dict()}")

# Redis Config
redis_config = {
    "host": params.general.redis_host,
    "port": params.general.redis_port,
    "decode_responses": True,
}

# Static File Definitions
static_dir = Path(__file__).parent / "static" / "ui"

# Main Sanic app definition
logger.debug(f"Static Files: {static_dir}")

app = Sanic(__name__)
app.static("/ui", str(static_dir))

logger.debug(app.config)

# Redis Cache Config
r_cache = aredis.StrictRedis(db=params.features.cache.redis_id, **redis_config)

# Sanic-Limiter Config
query_rate = params.features.rate_limit.query.rate
query_period = params.features.rate_limit.query.period
site_rate = params.features.rate_limit.site.rate
site_period = params.features.rate_limit.site.period
#
rate_limit_query = f"{query_rate} per {query_period}"
rate_limit_site = f"{site_rate} per {site_period}"
logger.debug(f"Query rate limit: {rate_limit_query}")
logger.debug(f"Site rate limit: {rate_limit_site}")

# Redis Config for Sanic-Limiter storage
r_limiter_db = params.features.rate_limit.redis_id
r_limiter_url = "redis://{host}:{port}/{db}".format(
    host=params.general.redis_host,
    port=params.general.redis_port,
    db=params.features.rate_limit.redis_id,
)
r_limiter = aredis.StrictRedis(db=params.features.rate_limit.redis_id, **redis_config)

# Adds Sanic config variable for Sanic-Limiter
app.config.update(RATELIMIT_STORAGE_URL=r_limiter_url)

# Initializes Sanic-Limiter
limiter = Limiter(app, key_func=get_remote_address, global_limits=[rate_limit_site])

# Prometheus Config
count_data = Counter(
    "count_data", "Query Counter", ["source", "query_type", "loc_id", "target"]
)

count_errors = Counter(
    "count_errors",
    "Error Counter",
    ["code", "reason", "source", "query_type", "loc_id", "target"],
)

count_ratelimit = Counter(
    "count_ratelimit", "Rate Limit Counter", ["message", "source"]
)

count_notfound = Counter(
    "count_notfound", "404 Not Found Counter", ["message", "path", "source"]
)


@app.route("/metrics")
@limiter.exempt
async def metrics(request):
    """Prometheus metrics"""
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    latest = generate_latest(registry)
    return response.text(
        latest,
        headers={
            "Content-Type": CONTENT_TYPE_LATEST,
            "Content-Length": str(len(latest)),
        },
    )


@app.exception(InvalidUsage)
async def handle_ui_errors(request, exception):
    """Renders full error page for invalid URI"""
    client_addr = get_remote_address(request)
    error = exception.args[0]
    status = error["status"]
    logger.info(error)
    count_errors.labels(
        status,
        code.get_reason(status),
        client_addr,
        request.json["query_type"],
        request.json["location"],
        request.json["target"],
    ).inc()
    logger.error(f'Error: {error["message"]}, Source: {client_addr}')
    return response.json(
        {"output": error["message"], "status": status, "keywords": error["keywords"]},
        status=status,
    )


@app.exception(ServerError)
async def handle_missing(request, exception):
    """Renders full error page for invalid URI"""
    logger.error(f"Error: {exception}")
    return response.json(exception, status=code.invalid)


@app.exception(NotFound)
async def handle_404(request, exception):
    """Renders full error page for invalid URI"""
    path = request.path
    html = render_html("404", uri=path)
    client_addr = get_remote_address(request)
    count_notfound.labels(exception, path, client_addr).inc()
    logger.error(f"Error: {exception}, Path: {path}, Source: {client_addr}")
    return response.html(html, status=404)


@app.exception(RateLimitExceeded)
async def handle_429(request, exception):
    """Renders full error page for too many site queries"""
    html = render_html("ratelimit-site")
    client_addr = get_remote_address(request)
    count_ratelimit.labels(exception, client_addr).inc()
    logger.error(f"Error: {exception}, Source: {client_addr}")
    return response.html(html, status=429)


@app.exception(ServerError)
async def handle_500(request, exception):
    """General Error Page"""
    client_addr = get_remote_address(request)
    count_errors.labels(500, exception, client_addr, None, None, None).inc()
    logger.error(f"Error: {exception}, Source: {client_addr}")
    html = render_html("500")
    return response.html(html, status=500)


async def clear_cache():
    """Function to clear the Redis cache"""
    try:
        await r_cache.flushdb()
        return "Successfully cleared cache"
    except Exception as error_exception:
        logger.error(f"Error clearing cache: {error_exception}")
        raise HyperglassError(f"Error clearing cache: {error_exception}")


@app.route("/", methods=["GET"])
@limiter.limit(rate_limit_site, error_message="Site")
async def site(request):
    """Main front-end web application"""
    return response.html(render_html("form", primary_asn=params.general.primary_asn))


@app.route("/test", methods=["GET"])
async def test_route(request):
    """Test route for various tests"""
    html = render_html("results")
    return response.html(html, status=500)


@app.route("/query", methods=["POST"])
@limiter.limit(rate_limit_query, error_message="Query")
async def hyperglass_main(request):
    """
    Main backend application initiator. Ingests Ajax POST data from
    form submit, passes it to the backend application to perform the
    filtering/lookups.
    """
    # Get JSON data from Ajax POST
    lg_data = request.json
    logger.debug(f"Unvalidated input: {lg_data}")

    # Return error if no target is specified
    if not lg_data["target"]:
        logger.debug("No input specified")
        raise handle_missing(request, params.messages.no_input)

    # Return error if no location is selected
    if lg_data["location"] not in devices.hostnames:
        logger.debug("No selection specified")
        raise handle_missing(request, params.messages.no_input)

    # Return error if no query type is selected
    if not Supported.is_supported_query(lg_data["query_type"]):
        logger.debug("No query specified")
        raise handle_missing(request, params.messages.no_input)

    # Get client IP address for Prometheus logging & rate limiting
    client_addr = get_remote_address(request)

    # Increment Prometheus counter
    count_data.labels(
        client_addr, lg_data["query_type"], lg_data["location"], lg_data["target"]
    ).inc()

    logger.debug(f"Client Address: {client_addr}")

    # Stringify the form response containing serialized JSON for the
    # request, use as key for k/v cache store so each command output
    # value is unique
    cache_key = str(lg_data)

    # Define cache entry expiry time
    cache_timeout = params.features.cache.timeout
    logger.debug(f"Cache Timeout: {cache_timeout}")

    # Check if cached entry exists
    if not await r_cache.get(cache_key):
        logger.debug(f"Sending query {cache_key} to execute module...")

        # Pass request to execution module
        starttime = time.time()
        try:
            cache_value = await Execute(lg_data).response()
        except (
            AuthError,
            RestError,
            ScrapeError,
            InputInvalid,
            InputNotAllowed,
        ) as backend_error:
            raise InvalidUsage(backend_error.__dict__())

        endtime = time.time()
        elapsedtime = round(endtime - starttime, 4)

        if not cache_value:
            raise handle_ui_errors(request, params.messages.request_timeout)

        logger.debug(
            f"Execution for query {cache_key} took {elapsedtime} seconds to run."
        )

        # Create a cache entry
        await r_cache.set(cache_key, str(cache_value))
        await r_cache.expire(cache_key, cache_timeout)

        logger.debug(f"Added cache entry for query: {cache_key}")

    # If it does, return the cached entry
    cache_response = await r_cache.get(cache_key)

    # Serialize stringified tuple response from cache
    # serialized_response = literal_eval(cache_response)
    # response_output, response_status = serialized_response
    response_output = cache_response

    logger.debug(f"Cache match for: {cache_key}, returning cached entry")
    logger.debug(f"Cache Output: {response_output}")

    return response.json({"output": response_output}, status=200)
