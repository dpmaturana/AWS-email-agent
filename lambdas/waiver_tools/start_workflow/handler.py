"""
Tool Lambda Handler
====================
Single Lambda entry point that wraps the three @tool functions for
standalone Lambda invocation (as opposed to in-process Strands calls).

Agent 2 (Person 2) can invoke these via Lambda:InvokeFunction, or
call them in-process by importing tools.py directly.

Routing is by the "tool" key in the event payload.

Event shapes:

  start_waiver_workflow:
    { "tool": "start_waiver_workflow",
      "waiver_id": "...", "email_from": "...", "department": "...",
      "waiver_type": "...", "collected_info": {}, "missing_fields": [] }

  update_waiver_state:
    { "tool": "update_waiver_state",
      "waiver_id": "...", "new_info": {}, "missing_fields": [] }

  get_waiver_state:
    { "tool": "get_waiver_state", "waiver_id": "..." }
"""

import json
import logging

from tools import start_waiver_workflow, update_waiver_state, get_waiver_state

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

TOOL_MAP = {
    "start_waiver_workflow": start_waiver_workflow,
    "update_waiver_state":   update_waiver_state,
    "get_waiver_state":      get_waiver_state,
}


def handler(event: dict, context) -> dict:
    # Unwrap API Gateway proxy if needed
    if "body" in event:
        try:
            event = json.loads(event["body"] or "{}")
        except (json.JSONDecodeError, TypeError):
            return {"statusCode": 400, "body": json.dumps({"error": "Invalid JSON body"})}

    tool_name = event.get("tool", "").strip()
    if not tool_name:
        return {"statusCode": 400, "body": json.dumps({"error": "'tool' key required"})}

    fn = TOOL_MAP.get(tool_name)
    if fn is None:
        return {
            "statusCode": 400,
            "body": json.dumps({"error": f"Unknown tool '{tool_name}'",
                                 "available": list(TOOL_MAP.keys())}),
        }

    # Build kwargs — exclude the "tool" key
    kwargs = {k: v for k, v in event.items() if k != "tool"}

    try:
        result = fn(**kwargs)
        return {"statusCode": 200, "body": json.dumps({"result": result})}
    except TypeError as exc:
        logger.error("Bad arguments for %s: %s", tool_name, exc)
        return {"statusCode": 400, "body": json.dumps({"error": str(exc)})}
    except Exception as exc:
        logger.error("Tool %s raised: %s", tool_name, exc)
        return {"statusCode": 500, "body": json.dumps({"error": str(exc)})}
