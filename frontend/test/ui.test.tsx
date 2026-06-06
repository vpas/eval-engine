import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { AccBar, Delta, Provider, StatusPill } from "@/components/ui";

describe("primitives", () => {
  it("StatusPill renders the status with its class", () => {
    const { container, getByText } = render(<StatusPill status="running" />);
    expect(getByText("running")).toBeTruthy();
    expect(container.querySelector(".pill.running")).toBeTruthy();
  });

  it("StatusPill maps training → running pill class but keeps the label", () => {
    const { container, getByText } = render(<StatusPill status="training" />);
    expect(getByText("training")).toBeTruthy();
    expect(container.querySelector(".pill.running")).toBeTruthy();
  });

  it("AccBar shows a percent and bucketed fill class", () => {
    const { getByText, container } = render(<AccBar value={0.5} />);
    expect(getByText("50%")).toBeTruthy();
    expect(container.querySelector(".fill.low")).toBeTruthy(); // <0.6 → low
  });

  it("AccBar renders an em-dash for null", () => {
    const { getByText } = render(<AccBar value={null} />);
    expect(getByText("—")).toBeTruthy();
  });

  it("Provider derives the short provider code from the model id", () => {
    const { getByText } = render(<Provider id="openai/gpt-4o-mini" />);
    expect(getByText("AI")).toBeTruthy();
    expect(getByText("gpt-4o-mini")).toBeTruthy();
  });

  it("Provider treats a checkpoint ref as an in-house model", () => {
    const { getByText } = render(<Provider id="checkpoint:tr-x:64000" />);
    expect(getByText("A")).toBeTruthy();
  });

  it("Delta points the right way", () => {
    const down = render(<Delta value={-0.06} />);
    expect(down.container.querySelector(".delta.down")).toBeTruthy();
    const up = render(<Delta value={0.06} />);
    expect(up.container.querySelector(".delta.up")).toBeTruthy();
  });
});
