# Minimal Phoenix robot program for SystemCore on a Pi 5B

Deploy this once and Phoenix Tuner X can talk to the SystemCore: on SystemCore the Phoenix
diagnostic server is not an OS package, it is started by the Phoenix 6 library inside the
robot program as soon as one Phoenix device is constructed (`TestSubsystem` creates a
`TalonFXS` with CAN ID 2 on `CANBus.systemcore(0)` = `can_s0`, the first USB-CAN adapter).
While the robot is enabled, the subsystem's default command sends `CoastOut` to that Talon FXS
every loop (bridge off, shaft free); its neutral mode is written as Coast, and nothing else on
the device is touched (motor arrangement, limits and inversion stay as set in Tuner X).
Replace it with your real robot code whenever you like — this is only a stopgap.

Derived from BobcatRobotics/SystemCore-Clone's `ctre-commands-v2` example (WPILib BSD).

## Versions

| | |
| --- | --- |
| WPILib / GradleRIO | 2027.0.0-alpha-6 (the newest alpha CTRE has a Phoenix build for) |
| Phoenix 6 | 26.50.0-alpha-1 (`vendordeps/Phoenix6-26.50.0-alpha-1.json`) |
| JDK | 25 (`~/wpilib/2027/jdk` from the WPILib installer) |

WPILib 2027 Alpha 7 is what SystemCore build 210 officially asks for, but as of Sept 2026
there is no Phoenix release for Alpha 7 ("Alpha 6 and prior vendordeps will not work with
Alpha 7"). This Alpha 6 program is what was verified against build 210 on a Pi 5B; when
CTRE ships an Alpha 7 build, bump the GradleRIO version and the vendordep together.

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
