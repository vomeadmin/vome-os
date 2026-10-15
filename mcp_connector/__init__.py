"""
mcp_connector

Vome OS as a claude.ai connector.

WHY THIS EXISTS
---------------
The support sprint runs in a claude.ai project, and so does anyone else at
Vome drafting a reply to a customer. Everything that makes a reply right
lives here instead: the help centre index, the product's own UI strings, the
frontend route map, the Sprint Lessons Log, the ClickUp backlog and read-only
access to the repositories. A claude.ai project can reach none of it.

This package serves those sources as a remote MCP server, mounted on the
existing web service, so a project (anyone's, not just Sam's) can ask "does
this exist, where is it, and is it already logged" and get an answer from
the live sources rather than from memory.

WHAT IT IS NOT
--------------
It is application code, not kernel. It imports the support application's
modules freely (product_strings, clickup_search, kb_sync) and the kernel
(`vomeos.integrations.code_search`) through the permitted inward direction.
Nothing under `vomeos/` imports this package.

It cannot send anything to a customer. There is no Zoho write and no email
in this package, by construction. Drafting happens in claude.ai and a person
sends, which is rule 2 of CLAUDE.md.

LAYOUT
------
    config.py           every env var the connector reads
    store.py            OAuth clients, codes, tokens, and the audit log
    oauth.py            sign in with Google, restricted to company email
    product_source.py   vome-react / VomeApp source on a box with no checkout
    repos.py            which repository and which LIVE branch
    knowledge.py        Lessons Log + learned sections, ranked
    feature_requests.py the only ClickUp writes
    tools.py            the tools, as plain functions
    server.py           the MCP server and how it attaches to main.app
"""
