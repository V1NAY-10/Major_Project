"use client";
/**
 * Display units. The backend stores and edits everything in mm and degrees; this only formats numbers in the
 * session's display unit (mm or inch) and precision. Mirrors backend/app/units.py formatting.
 */
import { createContext, useContext } from "react";
import { SessionSettings } from "@/types/cad";

export const DEFAULT_SETTINGS: SessionSettings = {
  display_unit: "mm",
  precision: 3,
  tolerance: { length: 0.001, angle: 0.01 },
};

const MM_PER_IN = 25.4;

export function fmtNumber(x: number, digits: number): string {
  const s = x.toFixed(digits);
  return Number(s) === 0 ? s.replace(/^-/, "") : s;
}

export interface Units {
  settings: SessionSettings;
  unit: "mm" | "in";
  /** A length given in mm, e.g. "120.000 mm" or "4.7244 in". `bare` leaves the unit off. */
  len: (mm: number | undefined | null, bare?: boolean) => string;
  /** An angle in degrees, e.g. "135.00°". */
  ang: (deg: number | undefined | null) => string;
  /** A value of a dimension or side-effect row by kind. */
  value: (v: number | undefined | null, kind?: "length" | "angle" | "count" | "deg" | "mm") => string;
  /** A point in mm: "10.00, 20.00, 5.00 mm". */
  vec: (v: number[] | undefined | null) => string;
}

export function makeUnits(settings: SessionSettings = DEFAULT_SETTINGS): Units {
  const unit = settings.display_unit;
  const lenDigits = settings.precision + (unit === "in" ? 1 : 0);
  const conv = (mm: number) => (unit === "in" ? mm / MM_PER_IN : mm);
  const len = (mm: number | undefined | null, bare = false) =>
    mm === undefined || mm === null || Number.isNaN(mm) ? "—" : `${fmtNumber(conv(mm), lenDigits)}${bare ? "" : ` ${unit}`}`;
  const ang = (deg: number | undefined | null) =>
    deg === undefined || deg === null ? "—" : `${fmtNumber(deg, Math.max(settings.precision - 1, 1))}°`;
  return {
    settings,
    unit,
    len,
    ang,
    value: (v, kind = "length") =>
      v === undefined || v === null
        ? "—"
        : kind === "angle" || kind === "deg"
          ? ang(v)
          : kind === "count"
            ? String(v)
            : len(v),
    vec: (v) => (v ? `${v.map((x) => fmtNumber(conv(x), Math.max(lenDigits - 1, 1))).join(", ")} ${unit}` : "—"),
  };
}

export const UnitsContext = createContext<Units>(makeUnits());

export const useUnits = () => useContext(UnitsContext);
