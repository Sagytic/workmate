"""Connect to a Workmate publication using the official MCP Python SDK.

Set WORKMATE_MCP_URL (ending in /mcp/) and WORKMATE_MCP_KEY in your process
environment. The key is never printed, persisted, or added to a URL. With no
arguments the example lists allowed tools/resources. --tool NAME --arguments
'{"request":"..."}' calls a selected tool and polls an asynchronous run.
"""
import argparse
import asyncio
import json
import os
from urllib.parse import urlparse

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


async def main(args):
    url = os.environ.get('WORKMATE_MCP_URL', 'http://127.0.0.1:8765/mcp/')
    key = os.environ.get('WORKMATE_MCP_KEY', '')
    parsed = urlparse(url)
    if not key or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise SystemExit('Set WORKMATE_MCP_KEY and a clean WORKMATE_MCP_URL ending in /mcp/.')
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost')):
        raise SystemExit('Use HTTPS for a hosted endpoint, or HTTP on localhost only.')
    async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + key}, timeout=httpx2.Timeout(30, read=300)) as http:
        transport = streamable_http_client(url, http_client=http)
        async with Client(transport, mode=args.mode) as client:
            tools = await client.list_tools()
            resources = await client.list_resources()
            print(json.dumps({'protocol': client.protocol_version, 'tools': [t.name for t in tools.tools],
                              'resources': [str(r.uri) for r in resources.resources]}, ensure_ascii=False, indent=2))
            if args.resource:
                resource = await client.read_resource(args.resource)
                for content in resource.contents:
                    print(content.text)
            if args.tool:
                result = await client.call_tool(args.tool, json.loads(args.arguments))
                if result.is_error:
                    raise SystemExit('The tool rejected the request. Check the publication scope and argument schema.')
                data = result.structured_content
                if data and data.get('id') and data.get('status') in ('queued', 'running'):
                    for _ in range(120):
                        await asyncio.sleep(1)
                        result = await client.call_tool('get_run', {'run_id': data['id']})
                        if result.is_error:
                            raise SystemExit('The run is no longer accessible with this key.')
                        data = result.structured_content
                        if data.get('status') not in ('queued', 'running'):
                            break
                print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Use an explicitly scoped Workmate MCP publication.')
    parser.add_argument('--mode', choices=('auto', 'legacy'), default='auto')
    parser.add_argument('--tool')
    parser.add_argument('--arguments', default='{}')
    parser.add_argument('--resource')
    arguments = parser.parse_args()
    try:
        asyncio.run(main(arguments))
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        # Transport errors may contain headers: avoid displaying their repr.
        raise SystemExit('MCP connection failed. Check the server, URL, key validity, and selected items.') from None
