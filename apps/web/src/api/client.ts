import createClient from "openapi-fetch";
import type { paths } from "./schema.gen";

/** Typed API client. Types are generated from the backend OpenAPI (`make gen`). */
export const api = createClient<paths>({ baseUrl: "", credentials: "include" });
