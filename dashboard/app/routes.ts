import { index, route, type RouteConfig } from "@react-router/dev/routes";

// An explicit index gives the workspace root priority over the layout route.
export default [
  index("routes/dashboard.tsx", { id: "overview" }),
  route("*", "routes/dashboard.tsx"),
] satisfies RouteConfig;
