import type { Metadata, Viewport } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "GRID — used-car starting grid",
  description:
    "Ranks live Carvana inventory by price, mileage and vehicle history, with both report vendors merged pessimistically.",
};

export const viewport: Viewport = {
  themeColor: "#08090c",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="shell">
          <header className="masthead">
            <a className="wordmark" href="/">
              <span className="flag" aria-hidden="true" />
              <span>
                GRID
                <span className="tagline">every car on the grid, ranked</span>
              </span>
            </a>
          </header>
          {children}
        </div>
      </body>
    </html>
  );
}
