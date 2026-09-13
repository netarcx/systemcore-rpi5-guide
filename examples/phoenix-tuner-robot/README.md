# Minimal Phoenix robot program for SystemCore on a Pi 5B

Deploy this once and Phoenix Tuner X can talk to the SystemCore: on SystemCore the Phoenix
diagnostic server is not an OS package, it is started by the Phoenix 6 library inside the
robot program as soon as one Phoenix device is constructed (`Robot` creates a `TalonFXS` with
CAN ID 2 on `CANBus.systemcore(0)` = `can_s0`, the first USB-CAN adapter). It is a plain
`TimedRobot`: while the robot is enabled it calls `Unmanaged.feedEnable` and sends `CoastOut`
to that Talon FXS every loop (bridge off, shaft free); the neutral mode is written as Coast and
nothing else on the device is touched (motor arrangement, limits and inversion stay as set in
Tuner X). Replace it with your real robot code whenever you like — this is only a stopgap.

Derived from BobcatRobotics/SystemCore-Clone's `ctre-commands-v2` example (WPILib BSD).

## Versions

| | |
| --- | --- |
| WPILib / GradleRIO | 2027.0.0-alpha-7 — **required** by SystemCore build 210 |
| Phoenix 6 | 26.50.0-alpha-1 (`vendordeps/Phoenix6-26.50.0-alpha-1.json`, see note) |
| JDK | 25 (`~/wpilib/2027/jdk` from the WPILib installer) |

**Why Alpha 7 is not optional:** build 210's `MrcCommDaemon` only puts "enabled" into the
CAN heartbeat while the robot program feeds a watchdog over NetworkTables
(`/Netcomm/Control/WatchdogActive`). The Alpha 7 HAL does that; the Alpha 6 HAL does not, so
an Alpha 6 program runs, thinks it is enabled, but every CTRE device stays
`Robot Enable: Disabled` in Tuner X and the daemon eventually E-stops the robot.

**Phoenix note:** as of Sept 2026 CTRE has published no Phoenix build for Alpha 7, and
GradleRIO Alpha 7 refuses the Alpha 5/6 vendordep by its year field. The JSON here has
`wpilibYear` changed to `2027_alpha7` so the build runs; WPILib's check warns that this is
unsupported. It compiles and the program starts and talks to the devices on build 210. Swap in
CTRE's real Alpha 7 vendordep as soon as it exists.

## Build and deploy

```bash
export JAVA_HOME=~/wpilib/2027/jdk PATH=~/wpilib/2027/jdk/bin:$PATH
./gradlew build -x test
./gradlew deploy -PsystemcoreHost=10.0.0.167     # the Pi's address (LAN lease, or 172.30.0.1 on its AP)
```

The deploy logs in as `systemcore`/`systemcore`, copies the jar to
`/home/systemcore/wpilib/classpath`, the JNI libraries to
`/home/systemcore/wpilib/third-party/lib`, writes `/home/systemcore/robotCommand`, and
enables + starts `robot.service`. Team number lives in `.wpilib/wpilib_preferences.json`
(0 here; change it to yours).

## Check it worked

```bash
ssh systemcore@<pi> 'sudo journalctl -u robot -n 40 --no-pager'   # HAL init, Phoenix startup
ssh systemcore@<pi> 'sudo ss -ltnp | grep 1250'                    # Phoenix diagnostic server
curl "http://<pi>:1250/?action=getversion"                         # same thing, from your PC
```

Then in Tuner X, connect to the Pi's address (not the roboRIO team-number address).
