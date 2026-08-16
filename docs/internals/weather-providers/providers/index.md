# Providers

One page per registered provider. Each documents the provider id and factory,
credential requirement, which capabilities it implements, endpoints, request
and response shape, upstream quirks and coverage limits, and error handling.

Capability coverage is uneven by design: fifteen providers implement general
forecast capabilities, and the rest are specialists that implement one or two
(air quality, pollen, UV, tides, space weather, hazards). See
[../dispatch](../dispatch.md) for how the dispatcher ranks and falls back
across them.

## General forecast sources

openmeteo, openweathermap, nws, metno, accuweather, weatherapi, weatherbit,
visualcrossing, weatherkit, pirateweather, tomorrowio, weatherstack,
worldweatheronline, meteomatics, stormglass, eccc

## Air quality, pollen, UV

airnow, openaq, waqi, iqair, purpleair, google_pollen, pollendotcom,
currentuvindex

## Marine, space weather, hazards, astronomy

noaa_coops, tidecheck, swpc, nasapower, firms, nifc, gdacs, sunrisesunset

```{toctree}
:maxdepth: 1
:glob:

*
```
