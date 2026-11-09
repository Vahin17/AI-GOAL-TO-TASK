import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ARDO — Autonomous Resilient Directed Orchestrator",
  description: "AI Goal-to-Task Planning Engine with Context-Aware RAG, Semantic Memory, and Safety-Aware Execution.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className="min-h-full antialiased"
    >
      <body className="min-h-full flex flex-col font-sans mb-0">
        {children}
      </body>
    </html>
  );
}
