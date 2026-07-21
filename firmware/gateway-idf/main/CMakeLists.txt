# The `main` component implicitly requires every other component in the build,
# so it can #include the Arduino core, NimBLE, PubSubClient and WiFiManager
# without an explicit REQUIRES list.
idf_component_register(
    SRCS "main.cpp"
    INCLUDE_DIRS "."
)
