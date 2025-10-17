#!/usr/bin/env python3
import argparse
import json
import os
import sys
from typing import Dict, Generator, Iterable, List, Optional

import google.auth
from google.auth.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.discovery import Resource
from googleapiclient.errors import HttpError

# Scopes for reading Cloud Billing catalog
READONLY_SCOPES = [
    "https://www.googleapis.com/auth/cloud-billing.readonly",
]


def get_billing_catalog_client() -> Resource:
    """Create an authenticated Cloud Billing Catalog API client using ADC."""
    credentials, project_id = google.auth.default(scopes=READONLY_SCOPES)
    # cache_discovery=False avoids needing a writeable cache directory
    return build("cloudbilling", "v1", credentials=credentials, cache_discovery=False)


def iter_services(client: Resource, page_size: int = 500) -> Generator[Dict, None, None]:
    """Yield all catalog services."""
    request = client.services().list(pageSize=page_size)
    while request is not None:
        response = request.execute()
        for service in response.get("services", []):
            yield service
        request = client.services().list_next(previous_request=request, previous_response=response)


def iter_skus(
    client: Resource,
    service_id: str,
    page_size: int = 500,
    currency_code: Optional[str] = None,
) -> Generator[Dict, None, None]:
    """Yield all SKUs for a given service id (e.g., services/6F81-5844-456A)."""
    parent = f"services/{service_id}" if not service_id.startswith("services/") else service_id
    params: Dict[str, object] = {"parent": parent, "pageSize": page_size}
    if currency_code:
        params["currencyCode"] = currency_code

    request = client.services().skus().list(**params)
    while request is not None:
        response = request.execute()
        for sku in response.get("skus", []):
            yield sku
        request = client.services().skus().list_next(previous_request=request, previous_response=response)


def service_matches_filter(service: Dict, name_substring: Optional[str]) -> bool:
    if not name_substring:
        return True
    haystack = f"{service.get('name', '')} {service.get('displayName', '')}".lower()
    return name_substring.lower() in haystack


def sku_matches_filters(
    sku: Dict,
    name_substring: Optional[str],
    resource_family: Optional[str],
    resource_group: Optional[str],
    usage_type: Optional[str],
    region: Optional[str],
) -> bool:
    if name_substring:
        haystack = " ".join(
            [
                sku.get("name", ""),
                sku.get("skuId", ""),
                sku.get("description", ""),
            ]
        ).lower()
        if name_substring.lower() not in haystack:
            return False

    category = sku.get("category", {})
    if resource_family and category.get("resourceFamily") != resource_family:
        return False
    if resource_group and category.get("resourceGroup") != resource_group:
        return False
    if usage_type and category.get("usageType") != usage_type:
        return False

    if region:
        regions: List[str] = sku.get("serviceRegions", [])
        if region not in regions and region.upper() not in regions:
            return False

    return True


def format_money(money: Dict) -> str:
    units = int(money.get("units", 0))
    nanos = int(money.get("nanos", 0))
    # nanos is 1e-9 of the currency unit
    return f"{units + nanos / 1_000_000_000:.9f}".rstrip("0").rstrip(".")


def extract_unit_price(sku: Dict) -> Optional[str]:
    pricing_info = sku.get("pricingInfo", [])
    if not pricing_info:
        return None
    pricing_expr = pricing_info[0].get("pricingExpression", {})
    tiered = pricing_expr.get("tieredRates")
    if tiered:
        rate = tiered[0].get("unitPrice")
        return format_money(rate) if rate else None
    unit_price = pricing_expr.get("usageUnit", None)
    if pricing_expr.get("unitPrice"):
        return format_money(pricing_expr["unitPrice"])  # type: ignore[index]
    # Fallback when unitPrice not present in expected place
    return None


def cmd_services(args: argparse.Namespace) -> int:
    client = get_billing_catalog_client()
    try:
        matched: List[Dict] = [s for s in iter_services(client, page_size=args.page_size) if service_matches_filter(s, args.filter)]
        if args.json:
            print(json.dumps(matched, indent=2))
            return 0
        # Human-readable table
        print(f"Found {len(matched)} services")
        print("service_id\tdisplay_name")
        for service in matched:
            # service["name"] looks like services/6F81-5844-456A
            name = service.get("name", "")
            service_id = name.split("/")[-1] if name else ""
            print(f"{service_id}\t{service.get('displayName', '')}")
        return 0
    except HttpError as e:
        print(f"HTTP error: {e}", file=sys.stderr)
        return 2


def cmd_skus(args: argparse.Namespace) -> int:
    client = get_billing_catalog_client()
    try:
        matched: List[Dict] = []
        for sku in iter_skus(client, service_id=args.service, page_size=args.page_size, currency_code=args.currency):
            if sku_matches_filters(
                sku,
                name_substring=args.filter,
                resource_family=args.resource_family,
                resource_group=args.resource_group,
                usage_type=args.usage_type,
                region=args.region,
            ):
                matched.append(sku)

        if args.json:
            print(json.dumps(matched, indent=2))
            return 0

        print(f"Found {len(matched)} SKUs in service {args.service}")
        print("sku_id\tcategory(resourceFamily/resourceGroup/usageType)\tregions\tusage_unit\tunit_price")
        for sku in matched:
            category = sku.get("category", {})
            cat_str = "/".join(
                [
                    category.get("resourceFamily", ""),
                    category.get("resourceGroup", ""),
                    category.get("usageType", ""),
                ]
            )
            regions = ",".join(sku.get("serviceRegions", []))
            pricing_info = sku.get("pricingInfo", [])
            usage_unit = pricing_info[0].get("pricingExpression", {}).get("usageUnit", "") if pricing_info else ""
            unit_price = extract_unit_price(sku) or ""
            print(f"{sku.get('skuId','')}\t{cat_str}\t{regions}\t{usage_unit}\t{unit_price}")
        return 0
    except HttpError as e:
        print(f"HTTP error: {e}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "GCP Cloud Billing Catalog CLI: list services and SKUs\n\n"
            "Authentication: Uses Application Default Credentials (ADC).\n"
            "- gcloud: run 'gcloud auth application-default login'\n"
            "- Workload Identity / Service Account: set GOOGLE_APPLICATION_CREDENTIALS to the JSON key path.\n\n"
            "Examples:\n"
            "  # List services, filter to compute\n"
            "  python tools/gcp_pricing_cli.py services --filter compute\n\n"
            "  # List SKUs for Compute Engine service (service id only)\n"
            "  python tools/gcp_pricing_cli.py skus --service 6F81-5844-456A --filter 'N1' --region us-central1\n\n"
            "  # Output JSON\n"
            "  python tools/gcp_pricing_cli.py services --json\n"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # services command
    p_services = sub.add_parser(
        "services",
        help="List catalog services",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p_services.add_argument("--filter", help="Case-insensitive substring match on name/displayName", default=None)
    p_services.add_argument("--page-size", type=int, default=500, help="Page size for API calls (default: 500)")
    p_services.add_argument("--json", action="store_true", help="Output raw JSON instead of table")
    p_services.set_defaults(func=cmd_services)

    # skus command
    p_skus = sub.add_parser(
        "skus",
        help=(
            "List SKUs for a service id (e.g., 6F81-5844-456A).\n"
            "Use --currency to select output currency. Apply client-side filters."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p_skus.add_argument("--service", required=True, help="Service id or 'services/<id>' (e.g., 6F81-5844-456A)")
    p_skus.add_argument("--currency", help="Currency code (e.g., USD, EUR)")
    p_skus.add_argument("--filter", help="Substring filter over name/skuId/description", default=None)
    p_skus.add_argument("--resource-family", help="Filter category.resourceFamily (e.g., Compute)")
    p_skus.add_argument("--resource-group", help="Filter category.resourceGroup (e.g., GCEGeneralPurpose)")
    p_skus.add_argument("--usage-type", help="Filter category.usageType (e.g., OnDemand)")
    p_skus.add_argument("--region", help="Require region in serviceRegions (e.g., us-central1)")
    p_skus.add_argument("--page-size", type=int, default=500, help="Page size for API calls (default: 500)")
    p_skus.add_argument("--json", action="store_true", help="Output raw JSON instead of table")
    p_skus.set_defaults(func=cmd_skus)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
