<template>
  <original-checkout :nodes="originalComponents" />
</template>

<script>
const STYLE_ID = "spark-hide-node-info-style"

const OriginalCheckout = {
  functional: true,
  props: {
    nodes: {
      type: [Array, Object],
      default: () => [],
    },
  },
  render(h, ctx) {
    const nodes = ctx.props.nodes
    if (!nodes || (Array.isArray(nodes) && nodes.length === 0)) {
      return h("div")
    }
    return h(
      "div",
      { style: "display: contents" },
      Array.isArray(nodes) ? nodes : [nodes]
    )
  },
}

function currentMethod(props) {
  if (props.method && props.method.currency) return props.method
  if (props.itemv && props.itemv.currency) return props.itemv
  return {}
}

function isSparkMethod(method) {
  return String((method && method.currency) || "").toLowerCase() === "spark"
}

function setSparkNodeInfoHidden(hidden) {
  if (typeof document === "undefined") return
  let style = document.getElementById(STYLE_ID)
  if (hidden) {
    if (!style) {
      style = document.createElement("style")
      style.id = STYLE_ID
      style.textContent = ".payment-box .v-tabs{display:none !important;}"
      document.head.appendChild(style)
    }
    const titles = document.querySelectorAll(".payment-box p")
    for (let i = 0; i < titles.length; i += 1) {
      if ((titles[i].textContent || "").trim() !== "Node Info") continue
      const wrap = titles[i].parentElement
      if (wrap) wrap.setAttribute("data-spark-hidden-node-info", "1")
      if (wrap) wrap.style.display = "none"
    }
    return
  }
  if (style && style.parentNode) style.parentNode.removeChild(style)
  const hiddenFields = document.querySelectorAll(
    '[data-spark-hidden-node-info="1"]'
  )
  for (let i = 0; i < hiddenFields.length; i += 1) {
    hiddenFields[i].style.display = ""
    hiddenFields[i].removeAttribute("data-spark-hidden-node-info")
  }
}

export default {
  components: {
    OriginalCheckout,
  },
  props: {
    originalComponents: {
      type: [Array, Object],
      default: () => [],
    },
    method: {
      type: Object,
      default: () => ({}),
    },
    itemv: {
      type: Object,
      default: () => ({}),
    },
  },
  computed: {
    sparkMethod() {
      return isSparkMethod(currentMethod(this))
    },
  },
  watch: {
    sparkMethod() {
      this.syncNodeInfo()
    },
  },
  mounted() {
    this.syncNodeInfo()
  },
  updated() {
    this.syncNodeInfo()
  },
  beforeDestroy() {
    setSparkNodeInfoHidden(false)
  },
  methods: {
    syncNodeInfo() {
      this.$nextTick(() => {
        setSparkNodeInfoHidden(this.sparkMethod)
      })
    },
  },
}
</script>
