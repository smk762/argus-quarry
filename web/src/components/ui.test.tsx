// Ext links third-party URLs from the tape: only http(s) may become a link.
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { Ext, safeHref } from "@/components/ui";

const render = (href: string) => renderToStaticMarkup(<Ext href={href}>source</Ext>);

describe("Ext", () => {
  it("links http and https URLs", () => {
    expect(render("https://commons.wikimedia.org/wiki/File:A.jpg")).toContain(
      'href="https://commons.wikimedia.org/wiki/File:A.jpg"',
    );
    expect(safeHref("http://example.org/x")).toBe("http://example.org/x");
  });

  it.each([
    "javascript:alert(document.domain)",
    " JavaScript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "/relative/path",
    "not a url",
  ])("renders %s as text, not a link", (href) => {
    const html = render(href);
    expect(html).not.toContain("<a");
    expect(html).not.toContain("href");
    expect(html).toContain("source");
  });
});
