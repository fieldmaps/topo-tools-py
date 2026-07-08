"""Non-user-configurable constants for the match pipeline."""

# Equal Earth -- used only to rank candidate parents by shared area (never
# stored); avoids biasing plurality assignment toward higher-latitude parents
# the way raw EPSG:4326 degree-area would. Matches the sister JS tool's choice.
EQUAL_AREA_CRS = "EPSG:8857"
