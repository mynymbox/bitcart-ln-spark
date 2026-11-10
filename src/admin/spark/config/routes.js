const Settings = () =>
  import("@Spark/pages/Settings.vue").then((m) => m.default || m)

export default [
  {
    name: "spark-settings",
    path: "/spark",
    component: Settings,
  },
]
