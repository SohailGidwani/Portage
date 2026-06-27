import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Portage",
  description: "Autonomous code-migration agent — control plane",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
