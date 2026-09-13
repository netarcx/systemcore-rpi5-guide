// Copyright (c) FIRST and other WPILib contributors.
// Open Source Software; you can modify and/or share it under the terms of
// the WPILib BSD license file in the root directory of this project.

package first.robot;

import com.ctre.phoenix6.CANBus;
import com.ctre.phoenix6.configs.MotorOutputConfigs;
import com.ctre.phoenix6.controls.CoastOut;
import com.ctre.phoenix6.hardware.TalonFXS;
import com.ctre.phoenix6.signals.NeutralModeValue;
import com.ctre.phoenix6.unmanaged.Unmanaged;
import org.wpilib.driverstation.RobotState;
import org.wpilib.framework.TimedRobot;

/**
 * Minimal robot program for a SystemCore on a Pi 5B: one Talon FXS (CAN ID 2 on can_s0). Its
 * existence starts the Phoenix diagnostic server that Tuner X connects to; while the robot is
 * enabled it is held in coast-out. Plain TimedRobot, no command framework.
 */
public class Robot extends TimedRobot {
  private static final int TALON_ID = 2;
  private static final int CAN_BUS = 0; // can_s0

  private final TalonFXS motor = new TalonFXS(TALON_ID, CANBus.systemcore(CAN_BUS));
  private final CoastOut coastOut = new CoastOut();
  private boolean wasEnabled;

  public Robot() {
    // Only the neutral mode is written; motor arrangement, limits and inversion set in Tuner X
    // are left alone (a full TalonFXSConfiguration would reset the arrangement to Disabled).
    var motorOutput = new MotorOutputConfigs();
    motor.getConfigurator().refresh(motorOutput);
    motorOutput.NeutralMode = NeutralModeValue.Coast;
    motor.getConfigurator().apply(motorOutput);
  }

  @Override
  public void robotPeriodic() {
    boolean enabled = RobotState.isEnabled();
    if (enabled) {
      // Phoenix's enable frame, alongside the FRC heartbeat the SystemCore sends.
      Unmanaged.feedEnable(100);
      motor.setControl(coastOut);
    }
    if (enabled != wasEnabled) {
      wasEnabled = enabled;
      System.out.println("[robot] DriverStation " + (enabled ? "ENABLED" : "DISABLED") + " mode="
          + RobotState.getRobotMode() + " phoenixEnable=" + Unmanaged.getEnableState());
    }
  }
}
