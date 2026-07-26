/**
 * One vehicle's archived report text, fetched on demand.
 *
 * Separate from the run payload on purpose: this is the bulky part, and most visitors open at
 * most one or two of them.
 */
import { NextRequest } from "next/server";

import { error, json } from "@/lib/auth";
import { sql } from "@/lib/db";

export const dynamic = "force-dynamic";

const VENDORS = new Set(["carfax", "autocheck"]);

export async function GET(request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  const params = request.nextUrl.searchParams;
  const vin = (params.get("vin") ?? "").trim().toUpperCase();
  const vendor = (params.get("vendor") ?? "").trim().toLowerCase();

  if (!vin) return error("vin is required", 400);
  if (!VENDORS.has(vendor)) return error("vendor must be carfax or autocheck", 400);

  const rows = (await sql`
    select body from run_reports
    where run_id = ${id}::uuid and vin = ${vin} and vendor = ${vendor}
  `) as { body: string }[];

  if (rows.length === 0) return error("no archived report for that vehicle", 404);

  return json({ vin, vendor, body: rows[0].body });
}
