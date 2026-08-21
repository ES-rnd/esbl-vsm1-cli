"""help command."""

from typing import Any


def cmd_help() -> Any:
    """
        Print the command reference.
    """

    print("""
        Commands:   
        scan                                Start background scanning
        stop_scan                           Stop background scanning
        list_devices                        List known devices

        connect -mac <ADDRESS>              Connect to a known device (TAB autocompletes)    
        disconnect                          (connected) Disconnect current device

        list_services                       (connected) List GATT services
        list_characteristics                (connected) List ESS (0000fe*) characteristics

        subscribe   -module <temp|imu|fft>  (connected) Read config + subscribe + live plot(s)
        unsubscribe -module <temp|imu|fft>  (connected) Stop notifications + close plots
          
        record      -module <fft> [-out <file.csv>]
                                            (subscribed) Start CSV recording (long format)
        stop_record -module <fft>           (subscribed) Stop CSV recording + close file

        update -module <fota> -file <*.bin> (connected) Performs Firmware Update Over the Air 

        configure -module temp -freq <1..10>
        configure -module imu  -low_scale <2g|4g|8g|16g> 
                               -high_scale <32g|64g|128g|256g>
                               -mode <low|high>
        
        configure -module fft  -axis <x|y|z>
                               -mode <low|high>
                                            (connected) Write sensor config (partial OK)

        calibrate -module <imu> -z_offset <10-100mg>
                                -mag_xy <10-100mg>
                                -jitter <10-100mg>
                                            (connected) Calibrate XY plane direction. The sensor
                                            must be placed so z-axis is close to zero
                                
        provision -wup <RTC_DISABLED|RTC_30_SECS|RTC_1_MIN|...>
                                            (connected) Set Wake-up Period for advertisement.
                                            RTC_DISABLED: stops period wake-up.

        clear                               Clear known devices JSON
        help                                Show this help
        exit                                Quit
    """)    # noqa
