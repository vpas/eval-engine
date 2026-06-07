import type { Metadata } from "next";
import "./globals.css";
import { AppBar } from "@/components/appbar";
import { QueryProvider } from "@/components/query-provider";

export const metadata: Metadata = {
  title: "eval-engine",
  description: "Distributed LLM evaluation",
};

// Fonts: the design system (globals.css) uses the system UI font + system mono — matching the UX
// prototype's GitHub-dark aesthetic — so no webfont is loaded here.
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="dense">
        <QueryProvider>
          <div className="app">
            <AppBar />
            <main>{children}</main>
          </div>
        </QueryProvider>
      </body>
    </html>
  );
}
