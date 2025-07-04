import time
import json
import os
import logging
import boto3
import base64

from datetime import datetime, timedelta
from boto3.dynamodb.conditions import Key, Attr
from services.google_ads_service import GoogleAdsService
from urllib.parse import parse_qsl, unquote

import services
import config

# .env constants
TABLE_PREFIX = os.getenv("TABLE_PREFIX", "default")
CLICK_LOG_TTL_MINUTES = int(os.getenv("CLICK_LOG_TTL_MINUTES", "15"))

# aws resources
dynamodb = boto3.resource("dynamodb")
click_log_table = dynamodb.Table(f"{TABLE_PREFIX}_click_logs")

# configs
kommo_config, google_ads_config = config.load_config()

# services
kommo_service = services.KommoService(config=kommo_config)
google_ads_service = services.GoogleAdsService(config=google_ads_config)

# logger
logger = logging.getLogger()
logger.setLevel("INFO")


def lambda_handler(event, context):
    logger.info("Incoming event: %s", json.dumps(event))

    path = event.get("rawPath", "/")
    method = event.get("requestContext")["http"]["method"]

    if path == "/outbound-click-logs" and method == "POST":
        return click_log_handler(event)

    if path == "/update-lead" and method == "POST":
        query_string_params = event.get("queryStringParameters", {})

        conversion_type_key = query_string_params.get("conversion_type")
        conversion_type = GoogleAdsService.ConversionType[
            conversion_type_key.upper()
        ]

        is_conversion_adjustment = (
            query_string_params.get("is_adjustment") == "True"
        )
        is_manual_import = query_string_params.get("is_manual") == "True"

        if is_conversion_adjustment:
            return upload_conversion_adjustment_handler(
                event=event,
                conversion_type=conversion_type,
            )

        if conversion_type == GoogleAdsService.ConversionType.MESSAGE_RECEIVED:
            if is_manual_import:
                return upload_conversion_handler(
                    event=event,
                    conversion_type=conversion_type,
                    lead_id=extract_incoming_lead_id(event),
                )
            return update_lead_handler(
                conversion_type=conversion_type, event=event
            )

        return upload_conversion_handler(
            event=event, conversion_type=conversion_type
        )

    return {"statusCode": 404, "message": "Invalid path"}


def click_log_handler(event):
    body = json.loads(event["body"] or {})
    gclid, gbraid = body.get("gclid"), body.get("gbraid")

    if not (gclid or gbraid):
        logger.error("Event object does not have gclid or gbraid field.")
        return {
            "statusCode": 400,
            "message": "Missing required parameter gclid and gbraid",
        }

    return persist_clicklog_to_db(body)


def update_lead_handler(conversion_type, event):
    response = click_log_table.query(
        KeyConditionExpression=Key("pk").eq("click"),
        FilterExpression=Attr("matched").eq(False),
        ScanIndexForward=False,
        Limit=1,
    )
    return update_lead(
        items=response.get("Items", []),
        conversion_type=conversion_type,
        lead_id=extract_incoming_lead_id(event),
    )


def upload_conversion_handler(event, conversion_type, lead_id=None):
    lead_id = extract_lead_id(event=event) if lead_id is None else lead_id

    try:
        google_ads_service.upload_offline_conversion(
            raw_lead=kommo_service.construct_raw_lead(lead_id=lead_id),
            conversion_type=conversion_type,
        )

        logger.info(
            "Successfully uploaded click conversion. Conversion type: %s",
            conversion_type.conversion_name,
        )

        return {
            "statusCode": 200,
            "message": "Conversion uploaded successfully.",
        }
    except RuntimeError as e:
        logger.error(
            "Something went wrong while persisting the click log. \
            Exception: %s",
            e,
        )

        return {
            "statusCode": 500,
            "message": "Something went wrong while persisting the click log.",
        }


def upload_conversion_adjustment_handler(event, conversion_type):
    try:
        lead_id = extract_incoming_lead_id(event)
        google_ads_service.upload_offline_conversion_adjustment(
            conversion_type=conversion_type, lead_id=lead_id
        )
        logger.info(
            "Successfully uploaded click conversion adjustment. Conversion type: %s",
            conversion_type.conversion_name,
        )

        return {
            "statusCode": 200,
            "message": "Conversion adjustment uploaded successfully.",
        }
    except RuntimeError as e:
        logger.error(
            "Something went wrong while uploading the click conversion adjustment. \
            Exception: %s",
            e,
        )

        return {
            "statusCode": 500,
            "message": "Something went wrong while uploading the click conversion adjustment.",
        }


def persist_clicklog_to_db(event):
    created_at = datetime.now()
    expires_at = created_at + timedelta(minutes=CLICK_LOG_TTL_MINUTES)

    try:
        click_log_table.put_item(
            Item={
                "pk": "click",
                "page_path": event.get("page_path"),
                "gclid": event.get("gclid"),
                "gbraid": event.get("gbraid"),
                "created_at": int(created_at.timestamp()),
                "expires_at": int(expires_at.timestamp()),
                "matched": False,
            }
        )

        logger.info(
            "Successfully persisted click log into table. gclid:%s",
            event.get("gclid"),
        )

        return {
            "statusCode": 200,
            "message": "Click log persisted successfully.",
        }
    except RuntimeError as e:
        logger.error(
            "Something went wrong while persisting the click log. \
            Exception: %s",
            e,
        )

        return {
            "statusCode": 500,
            "message": "Something went wrong while persisting the click log.",
        }


def update_lead(items, conversion_type, lead_id):
    if not items:
        try:
            kommo_service.update_lead(
                lead_id=lead_id,
                source="organic",
            )

            logger.info("Lead updated with organic source.")

            return {
                "statusCode": 200,
                "message": "Lead updated with organic source.",
            }
        except RuntimeError as e:
            logger.error(
                "Lead with organic source could not be updated. \
                         Exception: %s",
                e,
            )

            return {
                "statusCode": 500,
                "message": "Lead with organic source could not be updated.",
            }

    if datetime.now().timestamp() <= items[0]["expires_at"]:
        expires_at = items[0]["expires_at"]
        gclid, gbraid = items[0]["gclid"], items[0].get("gbraid")
        page_path = items[0]["page_path"]

        try:
            click_log_table.update_item(
                Key={"pk": "click", "expires_at": expires_at},
                UpdateExpression="SET matched = :matched",
                ExpressionAttributeValues={":matched": True},
            )

            kommo_service.update_lead(
                lead_id=lead_id,
                source="cpc",
                gclid=gclid,
                gbraid=gbraid,
                page_path=page_path,
            )

            logger.info("Lead updated with cpc source.")

            google_ads_service.upload_offline_conversion(
                raw_lead=kommo_service.construct_raw_lead(lead_id=lead_id),
                conversion_type=conversion_type,
            )

            return {
                "statusCode": 200,
                "message": "Lead updated with matched gclid.",
            }
        except RuntimeError as e:
            logger.error(
                "Lead with cpc source could not be updated. \
                         Exception: %s",
                e,
            )

            return {
                "statusCode": 500,
                "message": "Lead with cpc source could not be updated.",
            }


def extract_lead_id(event):
    body = event.get("body", {})

    decoded_str = base64.b64decode(body).decode("utf-8")
    query_str = unquote(decoded_str)
    payload = dict(parse_qsl(query_str))

    return payload["leads[status][0][id]"]


def extract_incoming_lead_id(event):
    body = event.get("body", {})

    decoded_str = base64.b64decode(body).decode("utf-8")
    query_str = unquote(decoded_str)
    payload = dict(parse_qsl(query_str))

    return payload["leads[add][0][id]"]
