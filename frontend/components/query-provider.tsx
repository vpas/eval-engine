"use client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

// One shared QueryClient for the app. Defaults tuned for a dashboard polling live backend state:
// keep the last good data on screen while refetching (no flash to a spinner), and don't refetch on
// every window focus (the per-query `refetchInterval` is the cadence that matters here).
export function QueryProvider({ children }: { children: React.ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            refetchOnWindowFocus: false,
            retry: 1,
            staleTime: 1000,
          },
        },
      }),
  );
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
