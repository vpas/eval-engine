import type { Metadata } from "next";
import { Archivo, JetBrains_Mono } from "next/font/google";
import Link from "next/link";
import "./globals.css";
import { UserChip } from "@/components/ui";

const display = Archivo({ subsets: ["latin"], variable: "--font-display", weight: ["400", "500", "600", "700"] });
const mono = JetBrains_Mono({ subsets: ["latin"], variable: "--font-mono", weight: ["400", "500", "600", "700"] });

export const metadata: Metadata = {
  title: "eval-engine",
  description: "Distributed LLM evaluation",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className={`${display.variable} ${mono.variable}`}>
        <div className="shell">
          <header className="topbar">
            <Link href="/" className="brand">
              <span className="dot" />
              eval<span className="slash">·</span>engine
            </Link>
            <span className="spacer" />
            <UserChip />
          </header>
          {children}
        </div>
      </body>
    </html>
  );
}
