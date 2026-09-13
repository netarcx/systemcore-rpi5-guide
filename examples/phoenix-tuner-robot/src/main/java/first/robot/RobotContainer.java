// Copyright (c) FIRST and other WPILib contributors.
// Open Source Software; you can modify and/or share it under the terms of
// the WPILib BSD license file in the root directory of this project.

package first.robot;

import first.robot.subsystems.TestSubsystem;
import org.wpilib.command2.Command;
import org.wpilib.command2.Commands;

/**
 * Minimal container: one subsystem that owns one Talon FXS. Constructing that device is what starts
 * the Phoenix diagnostic server (port 1250) that Tuner X connects to. The subsystem's default
 * command sends coast-out to the Talon FXS whenever the robot is enabled (default commands do not
 * run while disabled). No controller bindings and no autonomous.
 */
public class RobotContainer {
  private final TestSubsystem testSubsystem = new TestSubsystem();

  public RobotContainer() {
    testSubsystem.setDefaultCommand(testSubsystem.coast());
  }

  public Command getAutonomousCommand() {
    return Commands.none();
  }
}
