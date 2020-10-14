#!/usr/bin python3

# Imports
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# Python:
import logging
from datetime import datetime
from typing import Dict, Union, List
from functools import partial

# 3rd party:

# Internal:
from . import query_templates as queries
from ..caching import cache_client

try:
    from __app__.database import CosmosDB, Collection
except ImportError:
    from database import CosmosDB, Collection

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

__all__ = [
    'get_last_fortnight',
    'get_data_by_postcode',
    'get_msoa_data',
    'get_latest_value',
    'get_r_values'
]

ProcessedDateType = Dict[str, Union[str, datetime]]
NumericType = Union[int, float]
DatabaseValueType = Union[str, Union[str, NumericType, ProcessedDateType]]
DatabaseRowType = Union[
    Dict[str, DatabaseValueType],
    List[DatabaseValueType]
]
DatabaseOutputType = List[DatabaseRowType]

destination_metrics = {
    'testing': {
        "metric": 'newPCRTestsByPublishDate',
        "caption": "Testing",
        "heading": "PCR tests processed",
    },
    'healthcare': {
        "metric": 'newAdmissions',
        "caption": "Healthcare",
        "heading": "Patients admitted",
    },
    'cases': {
        "metric": 'newCasesByPublishDate',
        "caption": "Cases",
        "heading": "People tested positive",
    },
    'deaths': {
        "metric": 'newDeaths28DaysByPublishDate',
        "caption": "Deaths",
        "heading": "Deaths within 28 days of positive test",
    },

}

AreaTypeNames = {
    "nhsRegion": "Healthcare Region",
    "ltla": "Local Authority (Lower tier)",
    "utla": "Local Authority (Upper tier)",
    "region": "Region",
    "nation": "Nation"
}

data_db = CosmosDB(Collection.DATA)
lookup_db = CosmosDB(Collection.LOOKUP)
weekly_db = CosmosDB(Collection.WEEKLY)


def process_dates(date: str) -> ProcessedDateType:
    result = {
        'date': datetime.strptime(date, "%Y-%m-%d"),
    }

    result['formatted'] = result['date'].strftime('%-d %b %Y')

    return result


@cache_client.memoize(60 * 5)
def get_last_fortnight(timestamp: str, area_name: str, metric: str) -> DatabaseOutputType:
    """
    Retrieves the last fortnight worth of ``metric`` values
    for ``areaName`` as released on ``timestamp``.
    """
    query = queries.DataSinceApril.substitute({
        "metric": metric,
        "areaName": area_name
    })

    params = [
        {"name": "@releaseTimestamp", "value": timestamp},
        {"name": "@areaName", "value": area_name.lower()}
    ]

    result = [
        {**row, **process_dates(row["date"])}
        for row in data_db.query_iter(query, params=params)
    ]

    return result


@cache_client.memoize(60 * 60 * 12)
def get_latest_value(metric: str, timestamp: str, area_name: str):
    """
    Retrieves the latest ``metric`` value
    for ``areaName`` as released on ``timestamp``.
    """
    query = queries.LatestData.substitute({
        "metric": metric,
        "areaName": area_name
    })

    params = [
        {"name": "@releaseTimestamp", "value": timestamp},
        {"name": "@areaName", "value": area_name.lower()}
    ]

    result = data_db.query(query, params=params)
    
    return result[0]["value"]


@cache_client.memoize(60 * 60 * 12)
def get_postcode_areas(postcode):
    query = queries.PostcodeLookup

    params = [
        {"name": "@postcode", "value": postcode.replace(" ", "").upper()},
    ]

    return lookup_db.query(query, params=params)


@cache_client.memoize(60 * 60 * 6)
def get_r_values(latest_timestamp: str, area_name: str = "United Kingdom") -> Dict[str, dict]:
    get_latest = partial(get_latest_value, timestamp=latest_timestamp, area_name=area_name)

    result = {
        "transmissionRateMin": get_latest("transmissionRateMin"),
        "transmissionRateMax": get_latest("transmissionRateMax"),
        "transmissionRateGrowthRateMin": get_latest("transmissionRateGrowthRateMin"),
        "transmissionRateGrowthRateMax": get_latest("transmissionRateGrowthRateMax")
    }

    return result


@cache_client.memoize(60 * 60 * 12)
def get_data_by_code(area_code, timestamp):
    query = queries.LookupByAreaCode

    params = [
        {"name": "@areaCode", "value": area_code},
    ]

    result = lookup_db.query(query, params=params)
    try:
        location_data = result.pop()
    except IndexError:
        logging.critical(f"Missing lookup value for {params}")

    results = dict()

    for category, metric_data in destination_metrics.items():
        destination = location_data['destinations'].get(category)

        query = queries.DataByAreaCode.substitute(metric=metric_data["metric"])

        params = [
            {"name": "@seriesDate", "value": timestamp.split('T')[0]},
            {"name": "@releaseTimestamp", "value": timestamp},
            {"name": "@areaName", "value": destination['areaName'].lower()},
            {"name": "@areaType", "value": destination['areaType']},
        ]

        try:
            data = data_db.query(query, params=params)
            latest = data[0]
            area_type = latest.pop("areaType")

            results[category.capitalize()] = {
                "value": latest["value"],
                # "date": process_dates(latest.pop("date"))["formatted"],
                "areaType_formatted": AreaTypeNames[area_type],
                "areaType": area_type,
                "areaName": latest["areaName"],
                "data": [{**item, **process_dates(item['date'])} for item in data],
                **metric_data,
            }
        except IndexError:
            pass

    return results


@cache_client.memoize(60 * 60 * 6)
def get_msoa_data(postcode, timestamp):
    query = queries.MsoaData
    area = get_postcode_areas(postcode).pop()
    area_code = area['msoa']
    area_name = area['msoaName']

    params = [
        {"name": "@id", "value": f"MSOA|{area_code}"}
    ]

    try:
        data = weekly_db.query(query, params=params).pop()

        logging.info(data)
        cases_data = data["latest"]["newCasesBySpecimenDate"]

        response = {
            "areaName": area_name,
            "latestSum": cases_data["rollingSum"],
            "latestDate": process_dates(cases_data["date"])["formatted"],
            "dataTimestamp": timestamp
        }

        return response
    except (KeyError, IndexError):
        return None


def get_data_by_postcode(postcode, timestamp):
    # ToDo: Fail for invalid postcodes
    area = get_postcode_areas(postcode)
    area_code = area.pop()['ltla']

    return get_data_by_code(area_code, timestamp)
