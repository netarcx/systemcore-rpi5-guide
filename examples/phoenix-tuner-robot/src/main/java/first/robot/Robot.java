// Copyright (c) FIRST and other WPILib contributors.
// Open Source Software; you can modify and/or share it under the terms of
// the WPILib BSD license file in the root directory of this project.

package first.robot;

import org.wpilib.driverstation.DriverStation;
import org.wpilib.framework.TimedRobot;
import org.wpilib.system.DataLogManager;
import org.wpilib.command2.*;
import com.ctre.phoenix6.unmanaged.Unmanaged;
import org.wpilib.driverstation.RobotState;
/**
 * The methods in this class are called automatically corresponding to each mode, as described in
 * the TimedRobot documentation. If you change the name of this class or the package after creating
 * this project, you must also update the Main.java file in the project.
 */
public class Robot extends TimedRobot {
  private Command autonomousCommand;

  private final RobotContainer robotContainer;

  /**
   * This function is run when the robot is first started up and should be used for any
   * initialization code.
   */
  public Robot() {
    // Instantiate our RobotContainer.  This will perform all our button bindings, and put our
    // autonomous chooser on the dashboard.
    robotContainer = new RobotContainer();

    // Start recording to data log
    DataLogManager.start();

    // Record DS control and joystick data.
    // Change to `false` to not record joystick data.
    DriverStation.startDataLog(DataLogManager.getLog(), true);
  }

  /**
   * This function is called every 20 ms, no matter the mode. Use this for items like diagnostics
   * that you want ran during disabled, autonomous, teleoperated and test.
   *
   * <p>This runs after the mode specific periodic functions, but before LiveWindow and
   * SmartDashboard integrated updating.
   */
  private boolean wasEnabled;

  @Override
  public void robotPeriodic() {
    // Runs the Scheduler.  This is responsible for polling buttons, adding newly-scheduled
    // commands, running already-scheduled commands, removing finished or interrupted commands,
    // and running subsystem periodic() methods.  This must be called from the robot's periodic
    // block in order for anything in the Command-based framework to work.
    CommandScheduler.getInstance().run();

    // Tell Phoenix the robot is enabled so it sends its enable frame to the devices along with
    // the FRC heartbeat. On a roboRIO this is automatic; on the SystemCore alpha build it is
    // fed explicitly here every loop (100 ms timeout, so devices disable if the loop stalls).
    boolean enabled = RobotState.isEnabled();
    if (enabled) {
      Unmanaged.feedEnable(100);
    }
    if (enabled != wasEnabled) {
      wasEnabled = enabled;
      System.out.println("[robot] DriverStation " + (enabled ? "ENABLED" : "DISABLED") + " mode="
          + RobotState.getRobotMode() + " phoenixEnable=" + Unmanaged.getEnableState());
    }
  }
    if (enabled != wasEnabled) {
      wasEnabled = enabled;
      System.out.println("[robot] DriverStation " + (enabled ? "ENABLED" : "DISABLED")
          + (enabled ? " (" + (DriverStation.isAutonomous() ? "autonomous" : DriverStation.isTest() ? "test" : "teleop") + "), feeding Phoenix enable" : ""));
    }
  }

  /** This function is called once each time the robot enters Disabled mode. */
  @Override
  public void disabledInit() {}

  @Override
  public void disabledPeriodic() {}

  /** This autonomous runs the autonomous command selected by your {@link RobotContainer} class. */
  @Override
  public void autonomousInit() {
    autonomousCommand = robotContainer.getAutonomousCommand();

    // schedule the autonomous command (example)
    if (autonomousCommand != null) {
      CommandScheduler.getInstance().schedule(autonomousCommand);
    }
  }

  /** This function is called periodically during autonomous. */
  @Override
  public void autonomousPeriodic() {}

  @Override
  public void teleopInit() {
    // This makes sure that the autonomous stops running when
    // teleop starts running. If you want the autonomous to
    // continue until interrupted by another command, remove
    // this line or comment it out.
    if (autonomousCommand != null) {
      CommandScheduler.getInstance().cancel(autonomousCommand);
    }
  }

  /** This function is called periodically during operator control. */
  @Override
  public void teleopPeriodic() {}

  @Override
  public void utilityInit() {
    // Cancels all running commands at the start of utility mode.
    CommandScheduler.getInstance().cancelAll();
  }

  /** This function is called periodically during utility mode. */
  @Override
  public void utilityPeriodic() {}
}
