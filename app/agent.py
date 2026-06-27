# ruff: noqa
import os
import re
import json
import logging
import datetime
from typing import AsyncIterator, AsyncGenerator, Any
from pydantic import BaseModel, Field
from google.adk.agents import LlmAgent
from google.adk.tools import AgentTool
from google.adk.workflow import Workflow, START, Edge, node
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
import sys
from google.adk.agents.context import Context
from google.adk.apps import App, ResumabilityConfig
from google.genai import types
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

# Import our universal config
from .config import config

# Define MCP server toolset for stdio transport
mcp_toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable,
            args=["-m", "app.mcp_server"],
        )
    )
)

# PII detection patterns
CREDIT_CARD_PATTERN = re.compile(r'\b(?:\d[ -]*?){13,16}\b')
EMAIL_PATTERN = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b')

# Prompt injection patterns
INJECTION_KEYWORDS = [
    "ignore previous instructions", 
    "system prompt", 
    "override instructions", 
    "bypass safety", 
    "developer mode"
]

def log_audit(severity: str, event_type: str, details: dict):
    log_entry = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "severity": severity,
        "event_type": event_type,
        "details": details
    }
    print(f"AUDIT_LOG: {json.dumps(log_entry)}")

def make_content_event(text: str) -> Event:
    return Event(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=text)]
        )
    )

class WorkflowInput(BaseModel):
    query: str = Field(description="Inventory or purchase order request description")

class WorkflowState(BaseModel):
    query: str = ""
    sales_analysis: str = ""
    drafted_po: str = ""
    security_passed: bool = False
    security_log: str = ""
    po_approved: bool = False
    po_approval_comment: str = ""

# Define specialized LlmAgent sub-agents
sales_analyzer = LlmAgent(
    name="sales_analyzer",
    model=config.model,
    instruction="""You are a sales and inventory analyst.
Analyze historical sales records and current inventory levels to identify products that are running low or have high sales velocity.
Calculate the recommended replenishment quantities.
Use your tools to query inventory databases or sales history if available.
Always return your final recommendations clearly listing product IDs, names, current stock, and recommended quantities to order.""",
    description="Analyzes sales trends and current inventory to determine what items need to be restocked and in what quantities.",
    tools=[mcp_toolset]
)

purchase_order_drafter = LlmAgent(
    name="purchase_order_drafter",
    model=config.model,
    instruction="""You are a purchase order processing assistant.
Given a list of items and replenishment quantities, draft a formal Purchase Order (PO).
Include fields: PO Number, Date, Supplier Name, and a table of items (Product ID, Name, Quantity, Estimated Cost).
Ensure the format is clean, professional, and well-structured.
Use your tools to find supplier details and save the finalized purchase order.""",
    description="Drafts a formal purchase order given a list of products and quantities to restock.",
    tools=[mcp_toolset]
)

# Define coordinator/orchestrator agent
inventory_orchestrator = LlmAgent(
    name="inventory_orchestrator",
    model=config.model,
    instruction="""You are the inventory and purchasing coordinator.
Your task is to handle the user's request.
First, delegate to the sales_analyzer to find out which items need restocking.
Second, take the list of items from the sales_analyzer and delegate to the purchase_order_drafter to create a purchase order.
Once the purchase order is drafted, output the draft PO to the user.
Use the tools provided to communicate with these specialized sub-agents.
Do not make up data — rely on the results returned by your sub-agents.""",
    tools=[AgentTool(sales_analyzer), AgentTool(purchase_order_drafter)],
    output_key="drafted_po"
)

# Define workflow nodes
@node
async def security_checkpoint(ctx: Context, node_input: Any) -> AsyncGenerator[Event, None]:
    # Extract query string from node_input (handles raw string, types.Content, or dict)
    query = ""
    if isinstance(node_input, str):
        query = node_input
    elif isinstance(node_input, dict) and "query" in node_input:
        query = node_input["query"]
    elif hasattr(node_input, "parts") and node_input.parts:
        query = node_input.parts[0].text or ""
    elif hasattr(node_input, "query"):
        query = getattr(node_input, "query")
    else:
        query = str(node_input)
    
    # 1. Prompt Injection Detection
    detected_keywords = [kw for kw in INJECTION_KEYWORDS if kw in query.lower()]
    if detected_keywords:
        log_audit(
            severity="CRITICAL",
            event_type="PROMPT_INJECTION_DETECTED",
            details={"query": query, "matched_keywords": detected_keywords}
        )
        msg = "⚠️ Security Checkpoint Alert: Potential prompt injection detected. Blocked input."
        yield make_content_event(msg)
        yield Event(output=msg, route="fail", state={"security_passed": False, "security_log": "Prompt injection detected."})
        return
        
    # 2. PII Scrubbing
    scrubbed_query = query
    scrubbed_query = CREDIT_CARD_PATTERN.sub("[REDACTED_CC]", scrubbed_query)
    scrubbed_query = EMAIL_PATTERN.sub("[REDACTED_EMAIL]", scrubbed_query)
    
    pii_scrubbed = (scrubbed_query != query)
    
    # 3. Domain Specific Rule: Block commands related to deleting inventory
    if any(word in query.lower() for word in ["delete inventory", "wipe database", "truncate table", "clear stock"]):
        log_audit(
            severity="WARNING",
            event_type="UNAUTHORIZED_ACTION_BLOCKED",
            details={"query": query, "reason": "Attempted destructive inventory operation"}
        )
        msg = "⚠️ Security Checkpoint Alert: Destructive operations (like clearing stock) are unauthorized."
        yield make_content_event(msg)
        yield Event(output=msg, route="fail", state={"security_passed": False, "security_log": "Destructive operation blocked."})
        return
        
    log_audit(
        severity="INFO",
        event_type="SECURITY_CHECK_PASSED",
        details={"pii_scrubbed": pii_scrubbed}
    )
    
    yield Event(output=scrubbed_query, route="pass", state={"query": scrubbed_query, "security_passed": True, "security_log": "All checks passed."})

@node
async def po_approval_node(ctx: Context, node_input: Any) -> AsyncGenerator[Event, None]:
    # Check if we have received resume input for approval
    if not ctx.resume_inputs or "po_approval" not in ctx.resume_inputs:
        log_audit(
            severity="INFO",
            event_type="HITL_PAUSE",
            details={"message": "Waiting for purchase order approval decision."}
        )
        yield RequestInput(
            interrupt_id="po_approval",
            message="Please review the drafted purchase order. Do you approve? (Reply 'Yes' to approve or 'No' to reject)"
        )
        return
        
    decision = ctx.resume_inputs["po_approval"]
    log_audit(
        severity="INFO",
        event_type="HITL_RESPONSE_RECEIVED",
        details={"decision": decision}
    )
    
    if "yes" in decision.lower() or "approve" in decision.lower():
        yield Event(
            output=decision,
            route="approved",
            state={"po_approved": True, "po_approval_comment": decision}
        )
    else:
        yield Event(
            output=decision,
            route="rejected",
            state={"po_approved": False, "po_approval_comment": decision}
        )

@node
async def complete_order(ctx: Context, node_input: Any) -> AsyncGenerator[Event, None]:
    comment = ctx.state.get("po_approval_comment", "")
    log_audit(
        severity="INFO",
        event_type="PO_COMPLETED",
        details={"comment": comment}
    )
    msg = f"✅ Purchase Order completed and approved. Comment: '{comment}'. Processing replenishment now."
    yield make_content_event(msg)
    yield Event(output=msg)

@node
async def reject_order(ctx: Context, node_input: Any) -> AsyncGenerator[Event, None]:
    comment = ctx.state.get("po_approval_comment", "")
    log_audit(
        severity="WARNING",
        event_type="PO_REJECTED",
        details={"comment": comment}
    )
    msg = f"❌ Purchase Order was rejected. Reason: '{comment}'. No order will be drafted."
    yield make_content_event(msg)
    yield Event(output=msg)

@node
async def security_error_node(ctx: Context, node_input: Any) -> AsyncGenerator[Event, None]:
    log_audit(
        severity="WARNING",
        event_type="WORKFLOW_BLOCKED",
        details={"reason": ctx.state.get("security_log")}
    )
    msg = f"🚨 Workflow aborted due to security policy check: {ctx.state.get('security_log')}"
    yield make_content_event(msg)
    yield Event(output=msg)

# Define the workflow graph
root_agent = Workflow(
    name="inventory_workflow",
    description="Multi-agent workflow for analyzing sales levels and drafting secure purchase orders with human approval.",
    state_schema=WorkflowState,
    edges=[
        Edge(from_node=START, to_node=security_checkpoint),
        Edge(from_node=security_checkpoint, to_node=inventory_orchestrator, route="pass"),
        Edge(from_node=security_checkpoint, to_node=security_error_node, route="fail"),
        Edge(from_node=inventory_orchestrator, to_node=po_approval_node),
        Edge(from_node=po_approval_node, to_node=complete_order, route="approved"),
        Edge(from_node=po_approval_node, to_node=reject_order, route="rejected"),
    ]
)

# App wrapping the workflow
app = App(
    name="app",
    root_agent=root_agent,
    resumability_config=ResumabilityConfig(is_resumable=True)
)
