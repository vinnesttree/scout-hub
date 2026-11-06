import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import { ApiError } from "./api/client";
import "./styles.css";

// If any request comes back 401 (session expired / signed out elsewhere),
// re-check who we are so the UI drops back to read-only mode.
const onError = (err: unknown) => {
  if (err instanceof ApiError && err.status === 401) {
    queryClient.invalidateQueries({ queryKey: ["me"] });
  }
};

const queryClient: QueryClient = new QueryClient({
  queryCache: new QueryCache({ onError }),
  mutationCache: new MutationCache({ onError }),
  defaultOptions: {
    queries: {
      retry: (count, err) => !(err instanceof ApiError && err.status < 500) && count < 1,
      refetchOnWindowFocus: false,
    },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
