import { describe, expect, it } from "vitest";
import { fmtN, fmtCost, fmtStep, fmtTok, pct, ago } from "@/lib/api";

describe("formatting helpers", () => {
  it("fmtN abbreviates magnitudes", () => {
    expect(fmtN(950)).toBe("950");
    expect(fmtN(1500)).toBe("1.5k");
    expect(fmtN(2_100_000)).toBe("2.1M");
    expect(fmtN(3_000_000_000)).toBe("3.0B");
    expect(fmtN(null)).toBe("—");
  });

  it("fmtStep / fmtTok", () => {
    expect(fmtStep(500)).toBe("500");
    expect(fmtStep(64000)).toBe("64k");
    expect(fmtTok(134_400_000_000)).toBe("134B");
    expect(fmtTok(2_100_000)).toBe("2M");
  });

  it("pct rounds to integer percent", () => {
    expect(pct(0.6543)).toBe("65");
    expect(pct(1)).toBe("100");
    expect(pct(null)).toBe("—");
  });

  it("fmtCost is human-friendly", () => {
    expect(fmtCost(0)).toBe("$0");
    expect(fmtCost(2.5)).toBe("$2.50");
    expect(fmtCost(0.1234)).toBe("$0.1234");
  });

  it("ago renders relative time", () => {
    const t = new Date(Date.now() - 5 * 60_000).toISOString();
    expect(ago(t)).toBe("5m ago");
    expect(ago(null)).toBe("—");
  });
});
